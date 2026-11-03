"""Interactive worker adapters and an explicitly simulated fixture adapter."""

import hashlib
import json
import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

from .config import atomic_json
from .models import FactoryConfig, WorkerReport


class HarnessError(RuntimeError):
    def __init__(
        self,
        code: str,
        summary: str,
        *,
        uncertain: bool = False,
    ) -> None:
        super().__init__(summary)
        self.failure = {
            "code": code,
            "summary": summary,
            "source": "adapter",
            "uncertain": uncertain,
        }


def _config(value: FactoryConfig | dict) -> dict:
    return value.model_dump() if isinstance(value, FactoryConfig) else value


def _payload(value: dict) -> dict:
    result = value.get("result")
    return result if isinstance(result, dict) else value


def _assignment_path(assignment: dict) -> Path:
    explicit = assignment.get("assignment_path")
    if explicit:
        return Path(explicit)
    return (
        Path(assignment["home"])
        / "assignments"
        / assignment["id"]
        / "assignment.json"
    )


def _root_records(assignment: dict) -> list[dict]:
    records = []
    for root in assignment["roots"]:
        if isinstance(root, str):
            records.append({"name": Path(root).name, "path": root})
        else:
            records.append(root)
    return records


class PiHarness:
    """Use Core for terminals and Pi extensions for typed result delivery.

    Host execution is cooperative. A required sandbox is rejected before
    creating a terminal; a repository list is not an OS access boundary.
    """

    def __init__(
        self,
        config: FactoryConfig | dict,
        runner: Callable = subprocess.run,
    ) -> None:
        self.config = _config(config)
        self.runner = runner

    def _profile(self, role: str) -> dict:
        key = {
            "implementer": "worker",
            "reviewer": "reviewer",
            "qa": "qa_model",
        }.get(role, "worker")
        return self.config[key]

    def readiness(
        self,
        home: Path | str,
        role: str = "implementer",
        *,
        refresh: bool = False,
    ) -> dict:
        """Return only native readiness metadata, never a credential value."""
        profile = self._profile(role)
        arguments = [
            sys.executable,
            "-m",
            "pluto_factory.worker_launcher",
            str(home),
            "pi",
            "auth",
            "check",
            "--provider",
            profile["provider"],
            "--model",
            profile["model"],
            "--json",
        ]
        if not refresh:
            arguments.append("--no-refresh")
        try:
            result = self.runner(
                arguments,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            value = json.loads(result.stdout)
        except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
            return {
                "status": "invalid",
                "provider": profile["provider"],
                "reason": "credential_check_failed",
            }
        if (
            not isinstance(value, dict)
            or not isinstance(value.get("status"), str)
            or value.get("status") not in {"ready", "not_ready", "invalid"}
        ):
            return {
                "status": "invalid",
                "provider": profile["provider"],
                "reason": "unreadable_readiness_response",
            }
        reason = value.get("reason", "")
        if not isinstance(reason, str) or reason not in {
            "",
            "credentials_not_configured",
            "credentials_expired",
            "credentials_invalid",
            "provider_not_found",
            "model_not_found",
            "credentials_unavailable",
            "refresh_failed",
        }:
            reason = "" if value["status"] == "ready" else "unavailable"
        readiness = {
            "status": value["status"],
            "provider": profile["provider"],
            "reason": reason,
        }
        if isinstance(value.get("authType"), str) and value.get("authType") in {
            "oauth",
            "api_key",
            "ambient",
        }:
            readiness["auth_type"] = value["authType"]
        return readiness

    def _hc(self, args: list[str], *, mutating: bool = False) -> dict:
        try:
            result = self.runner(
                ["hc", *args],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
        except FileNotFoundError as error:
            raise HarnessError(
                "config.invalid", "Herdr Core executable was not found"
            ) from error
        except subprocess.TimeoutExpired as error:
            raise HarnessError(
                "transport.timeout",
                "Herdr Core did not return an operation result",
                uncertain=mutating,
            ) from error
        except OSError as error:
            raise HarnessError(
                "transport.unavailable",
                "Herdr Core operation could not be executed "
                f"({type(error).__name__})",
                uncertain=mutating,
            ) from error
        output = result.stdout.strip()
        error_output = result.stderr.strip()
        try:
            value = json.loads(output or error_output)
        except json.JSONDecodeError as error:
            raise HarnessError(
                "transport.invalid_response",
                "Herdr Core returned an unreadable operation result",
                uncertain=mutating,
            ) from error
        if not isinstance(value, dict):
            raise HarnessError(
                "transport.invalid_response",
                "Herdr Core operation did not return an object",
                uncertain=mutating,
            )
        value = _payload(value)
        if args[0] == "wait" and value.get("reason") in {
            "exit",
            "status",
            "timeout",
            "message",
            "not_started",
            "output",
        }:
            return value
        if result.returncode:
            problem = value.get("error", {})
            kind = problem.get("kind", "unavailable")
            if result.returncode == 124 and value.get("reason") == "timeout":
                return value
            code = {
                "blocked": "permission.required",
                "invalid_params": "config.invalid",
                "invalid_template": "config.invalid",
                "config": "config.invalid",
                "spawn_failed": "harness.spawn_failed",
                "not_found": "harness.not_found",
                "name_taken": "harness.name_taken",
            }.get(kind, "transport.unavailable")
            raise HarnessError(
                code,
                f"Herdr Core operation failed ({kind})",
                uncertain=mutating and kind in {"unavailable", "internal"},
            )
        return _payload(value)

    def _core_identity(self) -> str | None:
        identity = (
            self._hc(["server", "status", "--json"]).get("core", {}).get("id")
        )
        if not identity:
            raise HarnessError(
                "transport.invalid_response", "Herdr Core identity is missing"
            )
        return identity

    def _session(self, terminal: dict, assignment: dict) -> dict:
        address = terminal.get("address")
        if not address:
            raise HarnessError(
                "transport.invalid_response",
                "Herdr Core did not identify the created terminal",
                uncertain=True,
            )
        return {
            "kind": "pi",
            "address": address,
            "core_identity": self._core_identity(),
            "assignment_id": assignment["id"],
            "factory": self.config["name"],
            "owned": True,
            "assignment_path": str(_assignment_path(assignment)),
            "work_id": assignment["work_id"],
            "role": assignment["role"],
            "directory": str(Path(assignment["directory"]).resolve()),
            "roots": _root_records(assignment),
            "generation": assignment["generation"],
            "role_digest": hashlib.sha256(
                Path(assignment["role_path"]).read_bytes()
            ).hexdigest(),
            "model_profile": dict(self._profile(assignment["role"])),
        }

    def find_existing(self, assignment: dict) -> dict | None:
        value = self._hc(["ls", "--all", "--json"])
        matches = []
        for terminal in value.get("terminals", []):
            labels = terminal.get("labels", {})
            if (
                labels.get("factory") == self.config["name"]
                and labels.get("assignment") == assignment["id"]
                and labels.get("generation") == str(assignment["generation"])
            ):
                matches.append(terminal)
        if len(matches) > 1:
            raise HarnessError(
                "harness.ambiguous_assignment",
                "Multiple terminals claim this assignment; reconciliation needed",
                uncertain=True,
            )
        return self._session(matches[0], assignment) if matches else None

    def launch(self, assignment: dict) -> dict:
        if self.config.get("isolation", "required") != "cooperative":
            raise HarnessError(
                "config.invalid",
                "This prototype cannot enforce a host sandbox; required "
                "isolation was not downgraded",
            )
        existing = self.find_existing(assignment)
        if existing:
            return existing
        readiness = self.readiness(
            assignment["home"], assignment["role"], refresh=True
        )
        if readiness["status"] != "ready":
            raise HarnessError(
                "auth.required",
                "Pi provider authentication is not ready; use the native "
                "Pi login flow before dispatch",
            )
        assignment_path = _assignment_path(assignment)
        assignment = {**assignment, "assignment_path": str(assignment_path)}
        atomic_json(assignment_path, assignment)
        profile = self._profile(assignment["role"])
        name = f"pf-{assignment['id']}"[:32]
        labels = {
            "factory": self.config["name"],
            "work": assignment["work_id"],
            "assignment": assignment["id"],
            "generation": str(assignment["generation"]),
            "role": assignment["role"],
        }
        if self.config.get("herdr_space"):
            labels["herdr.space"] = self.config["herdr_space"]
        args = [
            "run",
            "--json",
            "--bg",
            "--no-parent",
            "--name",
            name,
            "--cwd",
            assignment["directory"],
        ]
        for key, value in labels.items():
            args.extend(["--label", f"{key}={value}"])
        tools = "read,grep,find,ls,factory_report"
        if assignment["role"] == "implementer":
            tools = "read,bash,edit,write,grep,find,ls,factory_report"
        args.extend(
            [
                "env",
                f"PLUTO_FACTORY_ASSIGNMENT={assignment_path}",
                f"PATH={os.environ.get('PATH', os.defpath)}",
                *[
                    f"{key}={os.environ[key]}"
                    for key in (
                        "PI_CODING_AGENT_DIR",
                        "PI_CODING_AGENT_SESSION_DIR",
                        "PI_PACKAGE_DIR",
                    )
                    if key in os.environ
                ],
                sys.executable,
                "-m",
                "pluto_factory.worker_launcher",
                assignment["home"],
                "pi",
                *(
                    ["--approve"]
                    if self.config.get("trust_registered_repositories")
                    else []
                ),
                "--provider",
                profile["provider"],
                "--model",
                profile["model"],
                "--thinking",
                profile["reasoning"],
                "--session-dir",
                assignment["session_dir"],
                "--name",
                f"{self.config['name']}: {assignment['role']}",
                "--no-extensions",
                "--no-mcp",
                "--tools",
                tools,
                "--append-system-prompt",
                assignment["role_path"],
                "--extension",
                str(Path(__file__).with_name("pi_extension.ts")),
                f"@{assignment['packet_path']}",
            ]
        )
        terminal = self._hc(args, mutating=True)
        try:
            return self._session(terminal, assignment)
        except HarnessError as error:
            error.failure["uncertain"] = True
            raise

    def reassign(self, session: dict, assignment: dict) -> dict:
        """Rebind a settled session without replaying uncertain submissions.

        Both sends have durable intent markers. An absent acknowledgment
        leaves reconciliation pending; it never causes an automatic resend.
        """
        if not session.get("owned"):
            raise HarnessError("scope.denied", "Terminal is not factory-owned")
        if (
            assignment["work_id"] != session.get("work_id")
            or assignment["role"] != session.get("role")
            or str(Path(assignment["directory"]).resolve())
            != session.get("directory")
            or assignment["generation"] <= session.get("generation", -1)
        ):
            raise HarnessError(
                "config.fresh_session_required",
                "Changed scope or generation requires a fresh worker session",
            )
        role_digest = hashlib.sha256(
            Path(assignment["role_path"]).read_bytes()
        ).hexdigest()
        if role_digest != session.get("role_digest"):
            raise HarnessError(
                "config.fresh_session_required",
                "Changed role prompt requires a fresh worker session",
            )
        if session.get("model_profile") != self._profile(assignment["role"]):
            raise HarnessError(
                "config.fresh_session_required",
                "Changed model profile requires a fresh worker session",
            )
        if session.get("core_identity") != self._core_identity():
            raise HarnessError(
                "harness.core_changed", "Core identity changed", uncertain=True
            )
        terminal = self._hc(["get", "--json", session["address"]])
        labels = terminal.get("labels", {})
        if labels.get("factory") != session["factory"] or labels.get(
            "assignment"
        ) not in {session["assignment_id"], assignment["id"]}:
            raise HarnessError("scope.denied", "Terminal ownership changed")
        filename = _assignment_path(assignment)
        operation_path = filename.with_name("reassign-operation.json")
        operation = (
            json.loads(operation_path.read_text())
            if operation_path.exists()
            else {"phase": "prepared"}
        )
        if operation["phase"] in {
            "task_sent",
            "complete",
        } and self._matches_started(assignment, binding=False):
            operation["phase"] = "complete"
            atomic_json(operation_path, operation)
            return self._session(terminal, assignment)
        status = terminal.get("status", {})
        status = status.get("state") if isinstance(status, dict) else status
        if status not in {"idle", "done"}:
            raise HarnessError(
                "harness.not_settled",
                "Session must be settled before assignment rebinding",
            )
        atomic_json(filename, assignment)
        if operation["phase"] == "prepared":
            operation["phase"] = "binding_sent"
            atomic_json(operation_path, operation)
            request = json.dumps({"assignment_path": str(filename)})
            self._hc(
                [
                    "send",
                    "--json",
                    session["address"],
                    f"/factory-bind {request}",
                ],
                mutating=True,
            )
        if not self._matches_started(assignment, binding=True):
            raise HarnessError(
                "harness.binding_pending",
                "New assignment binding is unconfirmed; do not resend",
                uncertain=True,
            )
        if operation["phase"] == "binding_sent":
            self._hc(
                [
                    "label",
                    "--json",
                    session["address"],
                    f"assignment={assignment['id']}",
                    f"generation={assignment['generation']}",
                ],
                mutating=True,
            )
            operation["phase"] = "bound"
            atomic_json(operation_path, operation)
        if operation["phase"] == "bound":
            operation["phase"] = "task_sent"
            atomic_json(operation_path, operation)
            value = self._hc(
                [
                    "send",
                    "--json",
                    session["address"],
                    f"@{assignment['packet_path']}",
                ],
                mutating=True,
            )
            if value.get("started") is True:
                operation["phase"] = "complete"
                atomic_json(operation_path, operation)
        if operation["phase"] != "complete" and not self._matches_started(
            assignment, binding=False
        ):
            raise HarnessError(
                "harness.submission_pending",
                "Revision submission is unconfirmed; do not resend",
                uncertain=True,
            )
        operation["phase"] = "complete"
        atomic_json(operation_path, operation)
        return self._session(terminal, assignment)

    def _matches_started(self, assignment: dict, *, binding: bool) -> bool:
        path = Path(assignment["started_path"])
        if not path.exists():
            return False
        try:
            value = json.loads(path.read_text())
        except json.JSONDecodeError:
            return False
        identity_matches = (
            value.get("assignment_id") == assignment["id"]
            and value.get("generation") == assignment["generation"]
        )
        if binding:
            return identity_matches and (
                value.get("bound") is True or value.get("task_started") is True
            )
        return identity_matches and value.get("task_started") is True

    def observe(self, session: dict, assignment: dict) -> dict:
        try:
            if session.get("core_identity") != self._core_identity():
                return {
                    "state": "unknown",
                    "failure": {
                        "code": "harness.core_changed",
                        "summary": "Core identity changed; reconcile before reuse",
                        "source": "runtime",
                        "uncertain": True,
                    },
                }
            value = self._hc(
                ["wait", "--json", "--timeout", "1", session["address"]]
            )
        except HarnessError as error:
            error.failure["uncertain"] = True
            return {"state": "unknown", "failure": error.failure}
        status = value.get("status")
        if isinstance(status, dict):
            status = status.get("state")
        if status == "blocked":
            return {
                "state": "blocked",
                "failure": {
                    "code": "permission.required",
                    "summary": "Worker is waiting for permission or input",
                    "source": "runtime",
                },
            }
        if value.get("reason") == "exit":
            observation = {
                "state": "finished",
                "exit_code": value.get("exit_code"),
            }
            if value.get("exit_code") not in (0, None) or value.get("signal"):
                observation["failure"] = {
                    "code": "harness.exited",
                    "summary": "Worker exited before successful settlement",
                    "source": "runtime",
                }
            return observation
        if status == "error":
            return {
                "state": "settled",
                "failure": {
                    "code": "harness.error",
                    "summary": "Worker settled with an execution error",
                    "source": "runtime",
                },
            }
        if not value.get("ended", True):
            return {"state": "working"}
        if status in {"idle", "done"}:
            if (
                not self._matches_started(assignment, binding=False)
                and not Path(assignment["report_path"]).exists()
            ):
                return {"state": "working", "startup_pending": True}
            return {"state": "settled"}
        return {"state": "unknown"}

    def _verify_owned(self, session: dict) -> None:
        if not session.get("owned"):
            raise HarnessError("scope.denied", "Terminal is not factory-owned")
        if session.get("core_identity") != self._core_identity():
            raise HarnessError(
                "harness.core_changed", "Core identity changed", uncertain=True
            )
        terminal = self._hc(["get", "--json", session["address"]])
        labels = terminal.get("labels", {})
        if (
            labels.get("assignment") != session["assignment_id"]
            or labels.get("factory") != session["factory"]
        ):
            raise HarnessError("scope.denied", "Terminal ownership changed")

    def interrupt(self, session: dict) -> dict:
        self._verify_owned(session)
        return self._hc(
            ["interrupt", "--json", session["address"]], mutating=True
        )

    def close(self, session: dict) -> None:
        self._verify_owned(session)
        self._hc(["kill", "--json", session["address"]], mutating=True)
        self._hc(["rm", "--json", session["address"]], mutating=True)


class FixtureHarness:
    """Deterministic examples, never a substitute for live model evidence."""

    def __init__(self, config: FactoryConfig | dict) -> None:
        self.config = _config(config)

    def _session_path(self, assignment: dict) -> Path:
        return _assignment_path(assignment).with_name("fixture-session.json")

    def find_existing(self, assignment: dict) -> dict | None:
        path = self._session_path(assignment)
        return json.loads(path.read_text()) if path.exists() else None

    def launch(self, assignment: dict) -> dict:
        existing = self.find_existing(assignment)
        if existing:
            return existing
        scenario = assignment.get("fixture_scenario") or "fix"
        marker = (
            Path(assignment["home"])
            / "fixture-state"
            / f"{assignment['work_id']}-{assignment['role']}-{scenario}.json"
        )
        first = not marker.exists()
        atomic_json(marker, {"seen": True})
        session = {
            "kind": "fixture",
            "address": f"fixture-{assignment['id']}",
            "assignment_id": assignment["id"],
            "owned": True,
            "state": "settled",
        }
        failure_scenario = first and assignment["role"] == "implementer"
        if failure_scenario and scenario == "crash_once":
            session.update(
                {
                    "state": "finished",
                    "failure": {
                        "code": "harness.exited",
                        "summary": "Injected worker crash",
                        "source": "runtime",
                    },
                }
            )
        elif failure_scenario and scenario == "idle_no_report_once":
            pass
        elif failure_scenario and scenario == "malformed_once":
            atomic_json(
                Path(assignment["report_path"]),
                {
                    "assignment_id": assignment["id"],
                    "generation": assignment["generation"],
                    "report": {"outcome": "ready"},
                },
            )
        else:
            report = WorkerReport(
                outcome="ready",
                summary="Simulated fixture completed",
                reviewed_repositories=[
                    r["name"] for r in _root_records(assignment)
                ],
            )
            if failure_scenario and scenario == "provider_once":
                report = WorkerReport(
                    outcome="error",
                    summary="Injected provider rate limit",
                    error_code="provider.rate_limited",
                    retry_after_seconds=0,
                )
            elif assignment["role"] == "implementer":
                self._apply_recipes(assignment)
            atomic_json(
                Path(assignment["report_path"]),
                {
                    "assignment_id": assignment["id"],
                    "generation": assignment["generation"],
                    "report": report.model_dump(),
                },
            )
        atomic_json(self._session_path(assignment), session)
        atomic_json(
            Path(assignment["started_path"]),
            {
                "assignment_id": assignment["id"],
                "generation": assignment["generation"],
                "fixture": True,
            },
        )
        return session

    def _apply_recipes(self, assignment: dict) -> None:
        for root in _root_records(assignment):
            if root.get("access", "write") != "write":
                continue
            directory = Path(root["path"]).resolve(strict=True)
            recipe_path = directory / "factory_fixture.json"
            if not recipe_path.exists():
                continue
            recipe = json.loads(recipe_path.read_text())
            for edit in recipe.get("edits", [recipe]):
                path = (directory / edit["file"]).resolve(strict=True)
                if not path.is_relative_to(directory):
                    raise HarnessError(
                        "scope.denied",
                        "Fixture recipe escaped its assigned root",
                    )
                content = path.read_text()
                for replacement in edit["replacements"]:
                    old, new = replacement["old"], replacement["new"]
                    if new in content:
                        continue
                    if content.count(old) != 1:
                        raise HarnessError(
                            "fixture.mismatch",
                            "Fixture replacement did not match once",
                        )
                    content = content.replace(old, new, 1)
                path.write_text(content)

    def observe(self, session: dict, assignment: dict) -> dict:
        return {
            key: session[key] for key in ("state", "failure") if key in session
        }

    def reassign(self, session: dict, assignment: dict) -> dict:
        return self.launch(assignment)

    def interrupt(self, session: dict) -> dict:
        return {"outcome": "stopped", "fixture": True}

    def close(self, session: dict) -> None:
        return None


def make_harness(config: FactoryConfig | dict):
    values = _config(config)
    if values["harness"] == "fixture":
        return FixtureHarness(values)
    return PiHarness(values)
