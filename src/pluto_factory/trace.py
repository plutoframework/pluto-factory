"""Opt-in factory debug trace for tracing generated defects to their source.

Workers append raw events (tool calls, assistant turns) to their assignment
folder. The factory process is the only writer of the canonical per-work trace
at ``logs/trace/<work_id>.jsonl``: it ingests worker events, mirrors durable
state events, records provenance and per-phase file changes, and redacts
known secrets before anything reaches the trace.
"""

import contextlib
import functools
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import __version__

RAW_NAME = "trace.raw.jsonl"
OFFSET_NAME = "trace.offset"
FINISHED_NAME = "trace.finished"
EDIT_TOOLS = {"edit", "write", "Edit", "Write", "MultiEdit", "NotebookEdit"}
SHELL_TOOLS = {"bash", "Bash"}
PATH_KEYS = ("file_path", "path", "notebook_path")
NOISY_EVENTS = {"worker.observed"}
GENERATOR = re.compile(r"(?:^|[\s;&|(])pluto(?:\s|$)")
SECRET_NAME = re.compile(r"KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL", re.IGNORECASE)
SECRET_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9_-]{16,}"),
    re.compile(r"(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"xox[abpr]-[A-Za-z0-9-]{10,}"),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]{16,}"),
    re.compile(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
        re.DOTALL,
    ),
)
REDACTED = "[REDACTED]"


def truncate(value, limit: int):
    """Bound every string in a JSON-like value to ``limit`` UTF-8 bytes."""
    if isinstance(value, str):
        encoded = value.encode()
        if len(encoded) <= limit:
            return value
        kept = encoded[:limit].decode(errors="ignore")
        return f"{kept}…[{len(encoded) - limit} bytes truncated]"
    if isinstance(value, dict):
        return {key: truncate(item, limit) for key, item in value.items()}
    if isinstance(value, list):
        return [truncate(item, limit) for item in value]
    return value


def append_line(path: Path, record: dict) -> None:
    """Append one JSON line with a single write; files stay owner-only."""
    path.parent.mkdir(parents=True, exist_ok=True)
    line = (json.dumps(record, default=str) + "\n").encode()
    descriptor = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.write(descriptor, line)
    finally:
        os.close(descriptor)


def trace_path(home: Path, work_id: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", work_id) or "_factory"
    return home / "logs" / "trace" / f"{safe}.jsonl"


def worker_event(assignment: dict, kind: str, **data) -> None:
    """Append a raw worker event when the assignment enables tracing."""
    settings = assignment.get("trace")
    if not settings:
        return
    record = {"ts": time.time(), "kind": kind, **data}
    append_line(
        Path(settings["path"]),
        truncate(record, settings["max_output_bytes"]),
    )


def _version(command: list[str]) -> str | None:
    if not shutil.which(command[0]):
        return None
    try:
        result = subprocess.run(
            command, capture_output=True, text=True, timeout=10, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None
    lines = (result.stdout or result.stderr).strip().splitlines()
    return lines[0] if lines else None


def pluto_dependencies(root: Path) -> dict:
    """Pluto library versions a worktree builds against."""
    found: dict = {}
    go_mod = root / "go.mod"
    if go_mod.is_file():
        modules = {}
        for line in go_mod.read_text().splitlines():
            match = re.search(
                r"(github\.com/plutoframework/\S+)\s+(v\S+)", line
            )
            if match:
                modules[match[1]] = match[2]
        if modules:
            found["go"] = modules
    candidates = [root / "package.json", *root.glob("*/package.json")]
    for path in candidates:
        if "node_modules" in path.parts or not path.is_file():
            continue
        try:
            manifest = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        packages = {
            name: version
            for table in ("dependencies", "devDependencies")
            for name, version in (manifest.get(table) or {}).items()
            if name.startswith("@plutoframework/")
        }
        if packages:
            found.setdefault("npm", {})[
                str(path.parent.relative_to(root)) or "."
            ] = packages
    pluto_toml = root / "pluto.toml"
    if pluto_toml.is_file():
        import tomllib

        try:
            manifest = tomllib.loads(pluto_toml.read_text())
            found["pluto_toml"] = {
                "schema_version": manifest.get("schema_version"),
                "kind": manifest.get("kind", "application"),
            }
        except (OSError, ValueError):
            pass
    return found


def changed_files(worktree: Path, base: str) -> list[dict]:
    """Files a phase changed relative to its starting commit."""

    def git(*arguments: str) -> str:
        result = subprocess.run(
            ["git", "-C", str(worktree), *arguments],
            capture_output=True,
            text=True,
            check=False,
        )
        return result.stdout if result.returncode == 0 else ""

    changes = []
    for line in git("diff", "--name-status", base).splitlines():
        status, _, path = line.partition("\t")
        if path:
            changes.append({"status": status, "path": path.split("\t")[-1]})
    for path in git("ls-files", "--others", "--exclude-standard").splitlines():
        if path:
            changes.append({"status": "A", "path": path})
    return changes


TRACE_ERRORS = (
    OSError,
    ValueError,
    TypeError,
    KeyError,
    RuntimeError,
    subprocess.SubprocessError,
)


def _never_fail(method):
    """A debug trace must never break factory work; report and continue."""

    @functools.wraps(method)
    def wrapper(self, *arguments, **keywords):
        try:
            return method(self, *arguments, **keywords)
        except TRACE_ERRORS as error:
            print(
                f"pluto-factory: debug trace skipped ({type(error).__name__}: "
                f"{error})",
                file=sys.stderr,
            )
            return None

    return wrapper


class Tracer:
    def __init__(self, home: Path, config):
        self.home = home
        self.config = config
        self.policy = config.debug
        self._secrets: list[str] | None = None
        self._versions: dict | None = None

    @property
    def enabled(self) -> bool:
        return self.policy.enabled

    def _known_secrets(self) -> list[str]:
        if self._secrets is None:
            values = {
                value
                for name, value in os.environ.items()
                if SECRET_NAME.search(name) and len(value) >= 8
            }
            from .credentials import credential_environment

            # Without readable credentials, env names and patterns still apply.
            with contextlib.suppress(OSError, RuntimeError, ValueError):
                values.update(
                    value
                    for value in credential_environment(self.home).values()
                    if value and len(value) >= 8
                )
            self._secrets = sorted(values, key=len, reverse=True)
        return self._secrets

    def redact(self, value):
        if not self.policy.redact:
            return value
        if isinstance(value, str):
            for secret in self._known_secrets():
                value = value.replace(secret, REDACTED)
            for pattern in SECRET_PATTERNS:
                value = pattern.sub(REDACTED, value)
            return value
        if isinstance(value, dict):
            return {key: self.redact(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self.redact(item) for item in value]
        return value

    @_never_fail
    def write(self, work_id: str, kind: str, source="factory", **data) -> None:
        if not self.enabled:
            return
        record = {
            "ts": data.pop("ts", None) or time.time(),
            "work_id": work_id,
            "source": source,
            "kind": kind,
            **data,
        }
        record = truncate(self.redact(record), self.policy.max_output_bytes)
        append_line(trace_path(self.home, work_id), record)

    @_never_fail
    def mirror(self, kind: str, subject: str, data) -> None:
        """Copy a durable state event into the trace."""
        if kind not in NOISY_EVENTS:
            self.write(subject, kind, source="state", data=data)

    def assignment_settings(self, directory: Path) -> dict | None:
        if not self.enabled:
            return None
        return {
            "path": str(directory / RAW_NAME),
            "max_output_bytes": self.policy.max_output_bytes,
        }

    def _tool_versions(self) -> dict:
        if self._versions is None:
            self._versions = {
                "pluto_factory": __version__,
                "pluto": _version(["pluto", "version"]),
                "pi": _version(["pi", "--version"]),
                "claude": _version(["claude", "--version"]),
                "go": _version(["go", "version"]),
                "node": _version(["node", "--version"]),
            }
        return self._versions

    @_never_fail
    def provenance(self, work: dict, run: dict) -> None:
        """Record what produced this assignment: models, prompts, versions."""
        if not self.enabled:
            return
        import hashlib

        assignment = run["assignment"]
        role = run["role"]
        profile_name = {"implementer": "worker", "reviewer": "reviewer"}.get(
            role, "qa_model"
        )
        role_text = Path(assignment["role_path"]).read_bytes()
        self.write(
            work["id"],
            "run.provenance",
            run_id=run["id"],
            role=role,
            harness=self.config.harness,
            model=getattr(self.config, profile_name).model_dump(),
            coordinator={
                "strategy": self.config.coordinator,
                "runtime": self.config.coordinator_runtime,
                "model": self.config.coordinator_model,
            },
            config_digest=assignment["config_digest"],
            role_prompt_sha256=hashlib.sha256(role_text).hexdigest(),
            tools=self._tool_versions(),
            repositories={
                entry["name"]: {
                    "access": entry["access"],
                    "base": entry.get("base"),
                    "candidate": entry.get("candidate"),
                    "pluto": pluto_dependencies(Path(entry["worktree"])),
                }
                for entry in work["data"].get("entries", [])
            },
        )

    @_never_fail
    def ingest(self, run: dict) -> None:
        """Move new raw worker events into the canonical trace."""
        settings = run["assignment"].get("trace")
        if not self.enabled or not settings:
            return
        raw = Path(settings["path"])
        if not raw.exists():
            return
        offset_path = raw.with_name(OFFSET_NAME)
        offset = (
            int(offset_path.read_text() or 0) if offset_path.exists() else 0
        )
        with raw.open("rb") as stream:
            stream.seek(offset)
            data = stream.read()
        complete = data[: data.rfind(b"\n") + 1]
        for line in complete.splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            kind = event.pop("kind", "worker.event")
            self.write(
                run["work_id"],
                kind,
                source="worker",
                run_id=run["id"],
                role=run["role"],
                **_annotate(event),
            )
        offset_path.write_text(str(offset + len(complete)))

    @_never_fail
    def finish_run(self, run: dict, work: dict) -> None:
        """Record per-phase file changes and transcript details once."""
        if not self.enabled:
            return
        marker = Path(run["assignment"]["assignment_path"]).with_name(
            FINISHED_NAME
        )
        if marker.exists():
            return
        self.ingest(run)
        for entry in work["data"].get("entries", []):
            changes = changed_files(Path(entry["worktree"]), entry["candidate"])
            self.write(
                work["id"],
                "phase.files",
                run_id=run["id"],
                role=run["role"],
                repository=entry["name"],
                worktree=entry["worktree"],
                access=entry["access"],
                base=entry["candidate"],
                changes=changes,
            )
        self._transcript(run)
        marker.write_text(str(time.time()))

    def _transcript(self, run: dict) -> None:
        started = Path(run["assignment"]["started_path"])
        try:
            session_file = json.loads(started.read_text()).get("session_file")
        except (OSError, ValueError):
            return
        if not session_file or not Path(session_file).is_file():
            return
        source = Path(session_file)
        lines = source.read_text(errors="replace").splitlines()
        if self.config.harness == "claude":
            self._claude_turns(run, lines)
        if self.policy.level == "full":
            copy = trace_path(self.home, run["work_id"]).with_suffix("")
            copy = copy / f"{run['id']}-{run['role']}.transcript.jsonl"
            for line in lines:
                try:
                    append_line(copy, self.redact(json.loads(line)))
                except ValueError:
                    continue
            self.write(
                run["work_id"],
                "transcript.copied",
                run_id=run["id"],
                role=run["role"],
                path=str(copy),
            )

    def _claude_turns(self, run: dict, lines: list[str]) -> None:
        """Claude assistant text and usage come from its native transcript."""
        usage: dict = {}
        for line in lines:
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            message = entry.get("message") or {}
            if entry.get("type") != "assistant":
                continue
            text = "\n".join(
                part.get("text", "")
                for part in message.get("content") or []
                if isinstance(part, dict) and part.get("type") == "text"
            ).strip()
            for key, value in (message.get("usage") or {}).items():
                if isinstance(value, int):
                    usage[key] = usage.get(key, 0) + value
            if text:
                self.write(
                    run["work_id"],
                    "assistant.message",
                    source="worker",
                    run_id=run["id"],
                    role=run["role"],
                    text=text,
                    model=message.get("model"),
                )
        if usage:
            self.write(
                run["work_id"],
                "run.usage",
                run_id=run["id"],
                role=run["role"],
                usage=usage,
            )


def _annotate(event: dict) -> dict:
    """Derive the touched path and generator flag from a tool call."""
    tool_input = event.get("input")
    if event.get("tool") and isinstance(tool_input, dict):
        for key in PATH_KEYS:
            if isinstance(tool_input.get(key), str):
                event["path"] = tool_input[key]
                break
        command = tool_input.get("command")
        if isinstance(command, str):
            event["command"] = command
            event["generator"] = bool(GENERATOR.search(command))
    return event


def latest_work(home: Path) -> str:
    """Work ID of the most recently written trace."""
    traces = sorted(
        (home / "logs" / "trace").glob("work-*.jsonl"),
        key=lambda path: path.stat().st_mtime,
    )
    if not traces:
        raise ValueError(
            "no debug traces yet; set [debug] enabled = true in factory.toml"
        )
    return traces[-1].stem


def read_trace(home: Path, work_id: str) -> list[dict]:
    path = trace_path(home, work_id)
    if not path.exists():
        raise ValueError(
            f"no trace for {work_id}; enable [debug] in factory.toml and "
            "run the work item"
        )
    records = []
    for line in path.read_text().splitlines():
        try:
            records.append(json.loads(line))
        except ValueError:
            continue
    return sorted(records, key=lambda record: record.get("ts", 0))


def attribute_files(records: list[dict]) -> list[dict]:
    """Map each changed file to the tool calls that plausibly produced it."""
    tools: dict[str, list[dict]] = {}
    for record in records:
        if record.get("kind") == "tool" and record.get("run_id"):
            tools.setdefault(record["run_id"], []).append(record)
    rows = []
    for record in records:
        if record.get("kind") != "phase.files":
            continue
        calls = tools.get(record["run_id"], [])
        shell = [call for call in calls if call.get("tool") in SHELL_TOOLS]
        worktree = record.get("worktree")
        for change in record.get("changes", []):
            targets = {change["path"]}
            if worktree:
                targets.add(str(Path(worktree) / change["path"]))
            direct = [
                call
                for call in calls
                if call.get("tool") in EDIT_TOOLS
                and isinstance(call.get("path"), str)
                and (
                    call["path"] in targets
                    or (
                        not worktree
                        and call["path"].endswith("/" + change["path"])
                    )
                )
            ]
            if direct:
                source = "edit"
                evidence = [call.get("tool") for call in direct]
            else:
                generators = [c for c in shell if c.get("generator")]
                source = "generator" if generators else "shell"
                evidence = [c.get("command") for c in (generators or shell)][
                    -5:
                ]
            rows.append(
                {
                    "run_id": record["run_id"],
                    "role": record.get("role"),
                    "repository": record.get("repository"),
                    "status": change["status"],
                    "path": change["path"],
                    "source": source if (direct or shell) else "unknown",
                    "evidence": evidence,
                }
            )
    return rows


WORKTREE_PREFIX = re.compile(r"^.*/worktrees/[^/]+/")


def _summary(record: dict) -> str:
    kind = record.get("kind", "")
    if kind == "tool":
        target = WORKTREE_PREFIX.sub(
            "", record.get("path") or record.get("command") or ""
        )
        flag = " [generator]" if record.get("generator") else ""
        error = " (error)" if record.get("is_error") else ""
        return f"{record.get('tool')} {target}".strip()[:160] + flag + error
    if kind == "phase.files":
        return (
            f"{record.get('repository')}: "
            f"{len(record.get('changes', []))} file(s) changed"
        )
    if kind == "assistant.message":
        return (record.get("text") or "").splitlines()[0][:160]
    if kind == "coordinator.decision":
        decision = (record.get("data") or {}).get("decision") or {}
        return f"{decision.get('action')}: {decision.get('rationale', '')}"[
            :160
        ]
    if kind == "evidence":
        data = record.get("data") or {}
        status = "passed" if data.get("passed") else "FAILED"
        command = data.get("command") or []
        if isinstance(command, list) and command:
            command = " ".join([Path(command[0]).name, *command[1:]])
        flags = " ".join(
            f"{key}={value}"
            for key, value in (data.get("environment") or {}).items()
        )
        parts = [data.get("kind"), data.get("repository"), command]
        text = " ".join(str(part) for part in parts if part)
        return f"{text} [{flags}] {status}" if flags else f"{text} {status}"
    if kind == "run.provenance":
        return f"{record.get('role')} via {record.get('harness')}"
    return ""


def render(home: Path, work_id: str, files: bool = False) -> dict:
    records = read_trace(home, work_id)
    return {
        "work_id": work_id,
        "trace": str(trace_path(home, work_id)),
        "events": records,
        "files": attribute_files(records) if files else None,
    }


def render_text(result: dict) -> str:
    records = result["events"]
    start = records[0]["ts"] if records else 0
    lines = [f"Trace {result['work_id']} ({result['trace']})", ""]
    for record in records:
        offset = record.get("ts", start) - start
        who = record.get("role") or record.get("source", "")
        lines.append(
            f"+{offset:8.1f}s  {who:<11} {record.get('kind', ''):<24} "
            f"{_summary(record)}".rstrip()
        )
    if result.get("files") is not None:
        lines.extend(["", "Files"])
        for row in result["files"]:
            evidence = "; ".join(str(item) for item in row["evidence"])
            lines.append(
                f"  {row['status']:<2} {row['repository']}/{row['path']}  "
                f"<- {row['role']} {row['source']}: {evidence}"[:200]
            )
    return "\n".join(lines)
