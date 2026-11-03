"""Bounded dashboard projections from private read-only ledger snapshots.

Known token/private-key patterns are masked; this is not comprehensive secret
recognition. Never project assignments, config bundles, prompts or process env.
"""

import hashlib
import json
import math
import re
import shlex
import sqlite3
import time
from pathlib import Path
from urllib.parse import urlsplit

from .dependencies import canonical_existing, graph
from .inventory import _snapshot, list_factories

MAX_WORK = 1000
TEXT_LIMIT = 12000
AGENT_OBSERVATION_MAX_AGE = 15
ANSI = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")
SECRET = re.compile(
    r"(?:sk-(?:proj-|ant-)?[A-Za-z0-9_-]{12,}|"
    r"gh[pousr]_[A-Za-z0-9_]{12,}|github_pat_[A-Za-z0-9_]{12,}|"
    r"xox[baprs]-[A-Za-z0-9-]{10,}|"
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?"
    r"(?:-----END [A-Z ]*PRIVATE KEY-----|$)|"
    r"(?i:bearer)\s+[A-Za-z0-9_.~+/-]{12,}=*|"
    r"(?i:(?:api[_-]?key|access[_-]?token|password|secret))"
    r"\s*[:=]\s*[^\s,;]+)",
    re.DOTALL,
)
SENSITIVE_KEY = re.compile(r"(?i:secret|token|password|credential|api.?key)")


def text(value, limit=TEXT_LIMIT):
    if value is None:
        return ""
    value = SECRET.sub("[REDACTED]", str(value))
    value = ANSI.sub("", value)
    value = "".join(c for c in value if c.isprintable() or c in "\n\t")
    return value[:limit] + ("\n[truncated]" if len(value) > limit else "")


def _object(value):
    parsed = json.loads(value) if value else {}
    return parsed if isinstance(parsed, dict) else {}


def _url(value):
    if not isinstance(value, str):
        return None
    parsed = urlsplit(value)
    if (
        parsed.scheme == "https"
        and parsed.netloc == "github.com"
        and not parsed.query
        and not parsed.fragment
        and re.fullmatch(
            r"/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/(?:pull|issues)/[0-9]+",
            parsed.path,
        )
    ):
        return value
    return None


def _source(item):
    key = canonical_existing(item.get("source_key"))
    match = re.fullmatch(r"github:([^:]+):(\d+)", key or "")
    return (
        _url(f"https://github.com/{match[1]}/issues/{match[2]}")
        if match
        else None
    )


def _read(database, selected=None):
    connection = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only=ON")
        if connection.execute("PRAGMA user_version").fetchone()[0] != 1:
            raise ValueError("unsupported dashboard schema")
        work = []
        for row in connection.execute(
            "SELECT id,title,phase,control,created,updated,source_key,payload,"
            "data,current_run,generation FROM work ORDER BY created DESC LIMIT ?",
            (MAX_WORK,),
        ):
            item = dict(row)
            item["payload"] = _object(item["payload"])
            item["data"] = _object(item["data"])
            work.append(item)
        paused = connection.execute(
            "SELECT value FROM settings WHERE key='paused'"
        ).fetchone()
        attention = {}
        decisions_seen = set()
        for row in connection.execute(
            "SELECT subject,kind,data,created FROM events WHERE kind IN "
            "('failure.observed','coordinator.decision') ORDER BY seq DESC LIMIT 2000"
        ):
            data = _object(row["data"])
            reason = data.get("summary")
            if row["kind"] == "coordinator.decision":
                if row["subject"] in decisions_seen:
                    continue
                decisions_seen.add(row["subject"])
                decision = data.get("decision")
                reason = (
                    decision.get("rationale")
                    if isinstance(decision, dict)
                    and decision.get("action") == "defer"
                    else None
                )
            previous = attention.get(row["subject"])
            if isinstance(reason, str) and (
                previous is None or row["created"] > previous["created"]
            ):
                attention[row["subject"]] = {
                    "message": text(reason),
                    "created": row["created"],
                }
        for row in connection.execute(
            "SELECT work_id,message,created FROM notifications WHERE kind='action_required' ORDER BY created DESC LIMIT 2000"
        ):
            previous = attention.get(row["work_id"])
            if previous is None or row["created"] > previous["created"]:
                attention[row["work_id"]] = {
                    "message": text(row["message"]),
                    "created": row["created"],
                }
        current_runs = {
            row["id"]: dict(row)
            for row in connection.execute(
                "SELECT id,work_id,role,state,generation FROM runs WHERE id IN (SELECT current_run FROM work WHERE current_run IS NOT NULL)"
            )
        }
        observations = {}
        for item in work:
            if item["current_run"] not in current_runs:
                continue
            # Take the latest work observation before checking its assignment.
            # An older working observation must never outlive a newer result.
            row = connection.execute(
                "SELECT data,created FROM events WHERE subject=? AND kind='worker.observed' ORDER BY seq DESC LIMIT 1",
                (item["id"],),
            ).fetchone()
            if row:
                observations[item["id"]] = {
                    "data": _object(row["data"]),
                    "created": row["created"],
                }
        result = {
            "work": work,
            "paused": bool(paused and paused[0] == "true"),
            "attention": attention,
            "roles": {
                run_id: run["role"] for run_id, run in current_runs.items()
            },
            "current_runs": current_runs,
            "observations": observations,
        }
        if selected is not None:
            result["notices"] = [
                dict(row)
                for row in connection.execute(
                    "SELECT message,created FROM notifications WHERE work_id=? AND kind='action_required' ORDER BY created DESC LIMIT 100",
                    (selected,),
                )
            ]
            result["events"] = [
                dict(row) | {"data": _object(row["data"])}
                for row in connection.execute(
                    "SELECT seq,kind,data,created FROM events WHERE subject=? AND kind != 'worker.observed' ORDER BY seq DESC LIMIT 100",
                    (selected,),
                )
            ]
            result["runs"] = [
                dict(row) | {"report": _object(row["report"])}
                for row in connection.execute(
                    "SELECT id,role,state,generation,report,created,updated FROM runs WHERE work_id=? ORDER BY created DESC LIMIT 40",
                    (selected,),
                )
            ]
            result["evidence"] = [
                dict(row) | {"data": _object(row["data"])}
                for row in connection.execute(
                    "SELECT id,data,created FROM evidence WHERE work_id=? ORDER BY created DESC LIMIT 80",
                    (selected,),
                )
            ]
        return result
    finally:
        connection.close()


def _agent_activity(item, snapshot, coordinator_running):
    """Reported activity, not an inference from assignment or workflow phase.

    Motion requires a matching current generation, a running run, a working
    observation at most 15 seconds old and a live coordinator observer lock.
    New, stale, stopped, paused, settled and completed workers stay static.
    """
    result = {
        "agent_working": False,
        "agent_state": "unknown",
        "agent_observed_at": None,
    }
    run = snapshot["current_runs"].get(item["current_run"])
    observation = snapshot["observations"].get(item["id"])
    matching_run = bool(
        run
        and run["work_id"] == item["id"]
        and run["generation"] == item["generation"]
    )
    matching_observation = bool(
        matching_run
        and observation
        and observation["data"].get("run_id") == item["current_run"]
    )
    if matching_observation:
        observed_at = observation["created"]
        if (
            isinstance(observed_at, (int, float))
            and math.isfinite(observed_at)
            and observed_at >= 0
        ):
            result["agent_observed_at"] = observed_at
    if snapshot["paused"] or item["control"] == "paused":
        return result | {"agent_state": "paused"}
    if item["phase"] == "completed":
        return result | {"agent_state": "finished"}
    if item["control"] in {"deferred", "quarantined", "backoff"}:
        return result | {"agent_state": item["control"]}
    if item["phase"] == "queued":
        return result | {"agent_state": "queued"}
    if not matching_run:
        return result
    if run["state"] != "running":
        state = (
            run["state"]
            if run["state"]
            in {"starting", "report_received", "settled", "finished", "failed"}
            else "unknown"
        )
        return result | {"agent_state": state}
    if not matching_observation:
        return result
    if observation["data"].get("startup_pending"):
        return result | {"agent_state": "starting"}
    if coordinator_running is not True:
        return result | {"agent_state": "observer_stopped"}
    observed_at = result["agent_observed_at"]
    if (
        observed_at is None
        or not 0 <= time.time() - observed_at <= AGENT_OBSERVATION_MAX_AGE
    ):
        return result | {"agent_state": "stale"}
    state = observation["data"].get("state")
    if state not in {
        "working",
        "idle",
        "settled",
        "finished",
        "blocked",
        "unknown",
    }:
        state = "unknown"
    return result | {
        "agent_state": state,
        "agent_working": state == "working"
        and not observation["data"].get("failure"),
    }


def _summary(item, snapshot, coordinator_running=False):
    indexed, edges = graph(snapshot["work"])
    dependencies = []
    for target in edges.get(item["id"], []):
        predecessor = indexed.get(target)
        state = "missing"
        if predecessor:
            state = (
                "completed"
                if predecessor["phase"] == "completed"
                else (
                    "blocked"
                    if predecessor["control"]
                    in {"paused", "deferred", "quarantined"}
                    else "waiting"
                )
            )
        dependencies.append(
            {
                "id": predecessor["id"] if predecessor else None,
                "title": text(
                    predecessor["title"] if predecessor else target, 500
                ),
                "state": state,
            }
        )
    return {
        **_agent_activity(item, snapshot, coordinator_running),
        "id": item["id"],
        "title": text(item["title"], 500),
        "phase": text(item["phase"], 100),
        "control": text(item["control"], 100),
        "created": item["created"],
        "updated": item["updated"],
        "source_url": _source(item),
        "dependencies": dependencies[:100],
        "active_role": snapshot["roles"].get(item["current_run"])
        if not snapshot["paused"]
        and item["phase"] != "completed"
        and item["control"] not in {"paused", "deferred", "quarantined"}
        else None,
        "pr_links": [
            {
                "title": text(
                    pr.get("title")
                    or f"{pr.get('repository', 'PR')} #{pr.get('number', '')}",
                    500,
                ),
                "url": _url(pr.get("url")),
                "state": text(pr.get("state", "unknown"), 100),
            }
            for pr in item["data"].get("pull_requests", [])[:30]
            if isinstance(pr, dict) and _url(pr.get("url"))
        ],
        "latest_failure": snapshot["attention"]
        .get(item["id"], {})
        .get("message")
        if item["phase"] != "completed"
        and item["control"] in {"deferred", "quarantined", "backoff"}
        and item["phase"] != "awaiting_merge"
        else None,
    }


def _event_message(row, snapshot):
    data = row["data"]
    for key in ("summary", "reason", "message", "rationale", "note"):
        if isinstance(data.get(key), str):
            return text(data[key], 3000)
    decision = data.get("decision")
    if isinstance(decision, dict) and isinstance(
        decision.get("rationale"), str
    ):
        return text(decision["rationale"], 3000)
    if row["kind"] == "work.deferred":
        nearby = [
            notice
            for notice in snapshot.get("notices", [])
            if -0.01 <= notice["created"] - row["created"] <= 2
        ]
        if nearby:
            return text(
                min(
                    nearby,
                    key=lambda notice: abs(notice["created"] - row["created"]),
                )["message"],
                3000,
            )
    return ""


class DashboardData:
    def __init__(self, root: Path, extra_home: Path | None = None):
        self.root = root.expanduser().resolve()
        self.extra_home = (
            extra_home.expanduser().resolve() if extra_home else None
        )

    def _entries(self):
        return list_factories(self.root, self.extra_home)

    @staticmethod
    def factory_id(home):
        return hashlib.sha256(str(home).encode()).hexdigest()[:20]

    def _home(self, factory_id):
        for entry in self._entries()["factories"]:
            home = Path(entry["location"])
            if self.factory_id(home) == factory_id:
                return home, entry
        raise KeyError("Factory was not found in the configured registry.")

    def state(self):
        registry = self._entries()
        factories = []
        for entry in registry["factories"][:100]:
            home = Path(entry["location"])
            projected = {
                key: entry.get(key)
                for key in (
                    "name",
                    "location",
                    "status",
                    "coordinator_running",
                    "models",
                    "repositories",
                    "last_activity",
                    "errors",
                )
            }
            projected["id"] = self.factory_id(home)
            projected["paused"] = entry["status"] == "paused"
            projected["work"] = []
            try:
                snapshot = _snapshot(home, _read)
                if snapshot:
                    projected["paused"] = snapshot["paused"]
                    projected["work"] = [
                        _summary(
                            item, snapshot, entry.get("coordinator_running")
                        )
                        for item in snapshot["work"]
                    ]
                    projected["work_truncated"] = (
                        len(snapshot["work"]) == MAX_WORK
                    )
            except (
                sqlite3.Error,
                OSError,
                ValueError,
                RuntimeError,
                TypeError,
                KeyError,
            ):
                projected["errors"] = [
                    *projected.get("errors", []),
                    "Work state is temporarily unavailable.",
                ]
            enabled = entry.get("integration") == "github"
            projected["watcher"] = {
                "enabled": enabled,
                "state": "paused"
                if projected["paused"]
                else (
                    "running"
                    if enabled and entry.get("coordinator_running")
                    else "stopped"
                ),
            }
            factories.append(projected)
        return {
            "schema_version": 1,
            "generated_at": time.time(),
            "root": str(self.root),
            "factories": factories,
            "errors": registry["errors"],
        }

    def detail(self, factory_id, work_id):
        home, entry = self._home(factory_id)
        snapshot = _snapshot(home, lambda database: _read(database, work_id))
        item = next(
            (
                item
                for item in (snapshot or {}).get("work", [])
                if item["id"] == work_id
            ),
            None,
        )
        if item is None:
            raise KeyError("Work item was not found.")
        summary = _summary(item, snapshot, entry.get("coordinator_running"))
        runs = snapshot["runs"]
        reports = []
        for run in runs:
            report = run["report"]
            if report:
                reports.append(
                    {
                        "id": run["id"],
                        "role": run["role"],
                        "outcome": text(report.get("outcome"), 100),
                        "summary": text(report.get("summary")),
                        "remaining_work": [
                            text(value, 2000)
                            for value in report.get("remaining_work", [])[:50]
                        ],
                        "created": run["updated"],
                        "artifacts": [
                            text(value, 500)
                            for value in report.get("artifacts", [])[:50]
                        ],
                    }
                )
        evidence = []
        for row in snapshot["evidence"]:
            data = row["data"]
            argv = data.get("command", [])
            command = (
                shlex.join([str(value) for value in argv])
                if isinstance(argv, list)
                else str(argv)
            )
            environment = data.get("environment", {})
            evidence.append(
                {
                    "id": row["id"],
                    "kind": text(data.get("kind"), 100),
                    "repository": text(data.get("repository"), 100),
                    "passed": data.get("passed") is True,
                    "command": text(command, 2000),
                    "environment": {
                        text(key, 100): "[REDACTED]"
                        if SENSITIVE_KEY.search(str(key))
                        else text(value, 500)
                        for key, value in environment.items()
                    }
                    if isinstance(environment, dict)
                    else {},
                    **{key: data.get(key) for key in ("exit_code", "seconds")},
                    "stdout": text(data.get("stdout")),
                    "stderr": text(data.get("stderr")),
                    "created": row["created"],
                }
            )
        command = [
            "pluto",
            "factory",
            "open",
            "--home",
            str(home),
            "--work",
            work_id,
        ]
        summary.update(
            {
                "instruction": text(item["payload"].get("instruction")),
                "acceptance": [
                    text(value, 2000)
                    for value in item["payload"].get("acceptance", [])[:100]
                ],
                "revision_reason": text(item["data"].get("revision_reason"))
                or None,
                "events": [
                    {
                        "seq": row["seq"],
                        "kind": text(row["kind"], 100),
                        "title": text(row["kind"].replace(".", " "), 100),
                        "message": _event_message(row, snapshot),
                        "created": row["created"],
                    }
                    for row in reversed(snapshot["events"])
                ],
                "reports": reports,
                "evidence": evidence,
                "herdr": {
                    "open_command": shlex.join(command),
                    "terminal_command": shlex.join([*command, "--terminal"]),
                    "available": self._viewable(home, work_id),
                },
                "runs": [
                    {
                        key: run[key]
                        for key in (
                            "id",
                            "role",
                            "state",
                            "generation",
                            "created",
                            "updated",
                        )
                    }
                    | {"terminal": None}
                    for run in runs
                ],
            }
        )
        return {
            "factory": {"id": factory_id, "name": text(entry["name"], 500)},
            "work": summary,
        }

    @staticmethod
    def _viewable(home, work_id):
        from .visibility import VisibilityError, current_worker

        try:
            return (
                current_worker(home, work_id=work_id).get("status") == "active"
            )
        except VisibilityError:
            return False

    def terminal(self, factory_id, work_id, *, runner=None):
        import subprocess
        from tempfile import TemporaryFile

        from .visibility import VisibilityError, _json_operation, current_worker

        home, _entry = self._home(factory_id)
        selected = current_worker(home, work_id=work_id)
        unavailable = {
            "available": False,
            "text": "",
            "captured_at": time.time(),
        }
        if (
            selected.get("status") != "active"
            or selected.get("work_id") != work_id
        ):
            return unavailable | {
                "message": "No current active worker belongs to this work item."
            }
        runner = runner or subprocess.run
        core = _json_operation(runner, ["hc", "server", "status", "--json"])
        if (core.get("core") or {}).get("id") != selected["core_identity"]:
            raise VisibilityError(
                "Herdr Core changed; terminal output was not read."
            )
        observed = _json_operation(
            runner, ["hc", "get", "--json", selected["terminal"]]
        )
        expected = {
            "factory": selected["factory"],
            "work": work_id,
            "assignment": selected["assignment_id"],
            "generation": str(selected["generation"]),
            "role": selected["role"],
        }
        if (
            observed.get("address") != selected["terminal"]
            or observed.get("state") != "live"
            or not isinstance(observed.get("labels"), dict)
            or any(
                observed["labels"].get(key) != value
                for key, value in expected.items()
            )
        ):
            raise VisibilityError(
                "Terminal ownership changed; output was not read."
            )
        with TemporaryFile(mode="w+b") as output:
            try:
                result = runner(
                    ["hc", "read", "--lines", "120", selected["terminal"]],
                    stdout=output,
                    stderr=subprocess.DEVNULL,
                    check=False,
                    timeout=10,
                )
            except (OSError, subprocess.SubprocessError) as error:
                raise VisibilityError(
                    "Herdr terminal output is unavailable."
                ) from error
            if result.returncode:
                raise VisibilityError("Herdr terminal output is unavailable.")
            output.seek(0)
            content = output.read(64000).decode("utf-8", errors="replace")
        latest = current_worker(home, work_id=work_id)
        if any(
            latest.get(key) != selected.get(key)
            for key in (
                "status",
                "work_id",
                "assignment_id",
                "generation",
                "terminal",
                "core_identity",
            )
        ):
            raise VisibilityError(
                "The active worker changed during capture; refresh before reading again."
            )
        return {
            "available": True,
            "text": text(content, 32000),
            "captured_at": time.time(),
            "attach_command": shlex.join(
                ["hc", "attach", selected["terminal"]]
            ),
            "peek_command": shlex.join(
                [
                    "herdr-app",
                    "do",
                    "terminal.peek",
                    "--terminal",
                    selected["terminal"],
                ]
            ),
        }
