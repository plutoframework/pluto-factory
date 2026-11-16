"""Assignment-bound reporting bridge for Claude Code workers.

Claude Code has no in-process extension API, so the Pi extension's duties are
split across a stdio MCP server (the typed ``factory_report`` tool) and
command hooks (startup markers, settlement and the report requirement).

    python -m pluto_factory.claude_worker mcp
    python -m pluto_factory.claude_worker hook <EventName>

Both read the assignment from ``PLUTO_FACTORY_ASSIGNMENT``.
"""

import json
import os
import re
import sys
import threading
import time
import uuid
from pathlib import Path

from pydantic import ValidationError

from .config import atomic_json
from .models import WRITER_ROLES, WorkerReport
from .trace import worker_event

SERVER = "factory"
TOOL = "factory_report"
QUALIFIED_TOOL = f"mcp__{SERVER}__{TOOL}"
CHECK_TOOL = "factory_check"
QUALIFIED_CHECK_TOOL = f"mcp__{SERVER}__{CHECK_TOOL}"
CHECK_ID = re.compile(r"^[0-9a-f]{32}$")
OUTPUT_LIMIT = 3000
PROTOCOL_VERSION = "2025-06-18"


def _assignment() -> dict:
    source = os.environ.get("PLUTO_FACTORY_ASSIGNMENT")
    if not source:
        raise SystemExit("Factory worker requires an assignment binding")
    return json.loads(Path(source).read_text())


def _identity(assignment: dict) -> dict:
    return {
        "assignment_id": assignment["id"],
        "generation": assignment["generation"],
    }


def _canonical(value) -> str:
    return json.dumps(value, sort_keys=True)


def _reported(assignment: dict) -> bool:
    path = Path(assignment["report_path"])
    if not path.exists():
        return False
    try:
        value = json.loads(path.read_text())
    except json.JSONDecodeError:
        return False
    return all(value.get(k) == v for k, v in _identity(assignment).items())


def _status(assignment: dict, state: str, **extra) -> None:
    atomic_json(
        Path(assignment["status_path"]),
        {
            **_identity(assignment),
            "state": state,
            "has_report": _reported(assignment),
            **extra,
        },
    )


def _started(assignment: dict, *, task_started: bool, **extra) -> None:
    atomic_json(
        Path(assignment["started_path"]),
        {**_identity(assignment), "task_started": task_started, **extra},
    )


def record_report(assignment: dict, arguments: dict) -> str:
    """Persist a validated report once; identical repeats are idempotent."""
    report = WorkerReport.model_validate(arguments).model_dump()
    path = Path(assignment["report_path"])
    if _reported(assignment):
        previous = json.loads(path.read_text())
        if _canonical(previous.get("report")) != _canonical(report):
            raise ValueError("A different final report is already recorded")
    else:
        atomic_json(path, {**_identity(assignment), "report": report})
    _status(assignment, "reported")
    return "Report persisted; coordinator validation is pending"


def _check_dir(assignment: dict) -> Path:
    return Path(assignment["assignment_path"]).parent / "checks"


def check_wait_seconds(assignment: dict) -> float:
    limit = (assignment.get("config") or {}).get("command_timeout_seconds", 900)
    return limit * 4 + 60


def pending_checks(assignment: dict) -> list[str]:
    """Request ids the worker has filed and the factory has not answered."""
    if "assignment_path" not in assignment:
        return []
    folder = _check_dir(assignment)
    if not folder.is_dir():
        return []
    found = []
    for path in sorted(folder.glob("*.request.json")):
        name = path.name.removesuffix(".request.json")
        if (
            CHECK_ID.match(name)
            and not (folder / f"{name}.result.json").exists()
        ):
            found.append(name)
    return found


def answer_check(assignment: dict, request_id: str, result: dict) -> None:
    if not CHECK_ID.match(request_id):
        raise ValueError("invalid check request id")
    atomic_json(_check_dir(assignment) / f"{request_id}.result.json", result)


def summarize_check(rows: list[dict]) -> dict:
    """Compact pass/fail view of validation evidence rows for the worker."""
    results = []
    for row in rows:
        output = "\n".join(
            part for part in (row.get("stdout"), row.get("stderr")) if part
        )
        results.append(
            {
                "repository": row["repository"],
                "command": row["command"],
                "environment": row.get("environment"),
                "passed": row["passed"],
                "exit_code": row["exit_code"],
                "timed_out": row.get("timed_out", False),
                "mutated_repositories": row.get("mutated_repositories", []),
                "seconds": round(row.get("seconds", 0), 1),
                "output_tail": output[-OUTPUT_LIMIT:],
            }
        )
    return {
        "passed": bool(rows) and all(row["passed"] for row in rows),
        "results": results,
    }


def check_status(assignment: dict, request_id: str, state: str) -> None:
    """Factory side: record that a request is ``waiting`` or ``running``."""
    atomic_json(
        _check_dir(assignment) / f"{request_id}.status.json",
        {"state": state, "at": time.time()},
    )


def _check_state(folder: Path, request_id: str) -> str | None:
    try:
        return json.loads((folder / f"{request_id}.status.json").read_text())[
            "state"
        ]
    except (OSError, ValueError, KeyError):
        return None


def check_queue_seconds(assignment: dict) -> float:
    """Longest a request may wait for a host check slot.

    The factory answers "check slot busy" before the phase deadline, so
    this only backstops a factory that stopped answering.
    """
    config = assignment.get("config") or {}
    phase = (
        config.get("implementer_phase_timeout_seconds")
        or config.get("phase_timeout_seconds")
        or 900
    )
    return phase + 120


_CHECK_GUARD = threading.Lock()


def request_check(assignment: dict, notify=None) -> dict:
    """File a check request and wait for the factory's answer.

    One check per worker at a time: a call made while another is in flight
    waits for and returns that run's result. Time spent queued for a host
    check slot does not count against the command timeout.
    """
    folder = _check_dir(assignment)
    folder.mkdir(parents=True, exist_ok=True)
    with _CHECK_GUARD:
        pending = pending_checks(assignment)
        if pending:
            request_id = pending[0]
        else:
            request_id = uuid.uuid4().hex
            atomic_json(
                folder / f"{request_id}.request.json",
                {**_identity(assignment), "requested": time.time()},
            )
    result_path = folder / f"{request_id}.result.json"
    queue_until = time.monotonic() + check_queue_seconds(assignment)
    run_until = None
    announced = False
    while True:
        if result_path.exists():
            try:
                return json.loads(result_path.read_text())
            except json.JSONDecodeError:
                pass
        state = _check_state(folder, request_id)
        if state == "waiting" and not announced and notify:
            announced = True
            notify("waiting for check slot")
        if state == "running" and run_until is None:
            run_until = time.monotonic() + check_wait_seconds(assignment)
            if notify:
                notify("running checks")
        limit = run_until if run_until is not None else queue_until
        if time.monotonic() >= limit:
            return {
                "passed": False,
                "error": "Timed out waiting for the factory",
            }
        time.sleep(0.5)


def _check_definition() -> dict:
    return {
        "name": CHECK_TOOL,
        "description": (
            "Run the repository's configured check commands (the same ones "
            "the factory runs in its checking phase) OUTSIDE your sandbox, "
            "against a snapshot of your current worktree. Use this instead "
            "of running make check, docker or database-backed tests "
            "yourself: they cannot run in your sandbox. Your files are not "
            "touched. Returns pass/fail with truncated output; the result "
            "is recorded as evidence. May take several minutes."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    }


def _tool_definition(assignment: dict) -> dict:
    schema = json.loads(Path(assignment["schema_path"]).read_text())
    return {
        "name": TOOL,
        "description": (
            "Persist the final typed result for this factory assignment. "
            "Call exactly once when the assignment is finished, including "
            "errors or remaining work."
        ),
        "inputSchema": schema,
    }


def handle_message(assignment: dict, message: dict, notify=None) -> dict | None:
    """Answer one JSON-RPC message; notifications return ``None``."""
    method = message.get("method")
    identifier = message.get("id")
    if identifier is None:
        return None
    if method == "initialize":
        params = message.get("params") or {}
        result = {
            "protocolVersion": params.get("protocolVersion", PROTOCOL_VERSION),
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "pluto-factory", "version": "1"},
        }
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        tools = [_tool_definition(assignment)]
        if assignment.get("role") in WRITER_ROLES:
            tools.append(_check_definition())
        result = {"tools": tools}
    elif method == "tools/call":
        params = message.get("params") or {}
        if (
            params.get("name") == CHECK_TOOL
            and assignment.get("role") in WRITER_ROLES
        ):
            outcome = request_check(assignment, notify)
            return {
                "jsonrpc": "2.0",
                "id": identifier,
                "result": {
                    "content": [
                        {"type": "text", "text": json.dumps(outcome, indent=2)}
                    ],
                    "isError": not outcome.get("passed"),
                },
            }
        if params.get("name") != TOOL:
            return {
                "jsonrpc": "2.0",
                "id": identifier,
                "error": {"code": -32602, "message": "Unknown tool"},
            }
        try:
            text = record_report(assignment, params.get("arguments") or {})
            result = {"content": [{"type": "text", "text": text}]}
        except (ValidationError, ValueError) as error:
            result = {
                "content": [{"type": "text", "text": str(error)}],
                "isError": True,
            }
    else:
        return {
            "jsonrpc": "2.0",
            "id": identifier,
            "error": {"code": -32601, "message": "Method not found"},
        }
    return {"jsonrpc": "2.0", "id": identifier, "result": result}


def serve(stdin=sys.stdin, stdout=sys.stdout) -> None:
    """Serve JSON-RPC on stdio.

    ``factory_check`` calls run on their own threads: a long ``factory_check`` must not
    block ``factory_report`` behind it, or the session ends unreported.
    """
    assignment = _assignment()
    write_lock = threading.Lock()

    def send(payload: dict) -> None:
        with write_lock:
            stdout.write(json.dumps(payload) + "\n")
            stdout.flush()

    def call(message: dict) -> None:
        token = ((message.get("params") or {}).get("_meta") or {}).get(
            "progressToken"
        )
        ticks = iter(range(1, 1000))

        def notify(text: str) -> None:
            if token is not None:
                send(
                    {
                        "jsonrpc": "2.0",
                        "method": "notifications/progress",
                        "params": {
                            "progressToken": token,
                            "progress": next(ticks),
                            "message": text,
                        },
                    }
                )

        response = handle_message(assignment, message, notify)
        if response is not None:
            send(response)

    calls: list[threading.Thread] = []
    for line in stdin:
        if not line.strip():
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            response = {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32700, "message": "Parse error"},
            }
        else:
            if (
                message.get("method") == "tools/call"
                and (message.get("params") or {}).get("name") == CHECK_TOOL
            ):
                thread = threading.Thread(
                    target=call, args=(message,), daemon=True
                )
                thread.start()
                calls.append(thread)
                continue
            response = handle_message(assignment, message)
        if response is not None:
            send(response)
    for thread in calls:
        thread.join(timeout=10)


def handle_hook(assignment: dict, event: str, payload: dict) -> dict | None:
    """Apply one Claude Code hook event; the result is printed as JSON."""
    _trace_hook(assignment, event, payload)
    if event in {"PostToolUse", "PostToolUseFailure"}:
        return None
    if event == "SessionStart":
        if (
            payload.get("source", "startup") == "startup"
            or not Path(assignment["started_path"]).exists()
        ):
            _started(
                assignment,
                task_started=False,
                session_file=payload.get("transcript_path"),
                model=payload.get("model"),
            )
            _status(assignment, "idle")
        return None
    if event == "UserPromptSubmit":
        _started(
            assignment,
            task_started=True,
            session_file=payload.get("transcript_path"),
        )
        _status(assignment, "working")
        return None
    if event == "PreToolUse":
        if _reported(assignment) and payload.get("tool_name") != QUALIFIED_TOOL:
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": (
                        "Assignment report is recorded; await coordinator input"
                    ),
                }
            }
        return None
    if event == "Stop":
        if not _reported(assignment) and not payload.get("stop_hook_active"):
            return {
                "decision": "block",
                "reason": (
                    f"The assignment requires a final {TOOL} tool call. "
                    "Report the actual outcome, including errors or "
                    "remaining work."
                ),
            }
        _status(assignment, "settled")
        return None
    if event == "SessionEnd":
        _status(assignment, "closed")
        return None
    return None


SESSION_EVENTS = {
    "SessionStart": "session.start",
    "UserPromptSubmit": "session.prompt",
    "Stop": "session.stop",
    "SessionEnd": "session.end",
}


def _trace_hook(assignment: dict, event: str, payload: dict) -> None:
    """Record hook payloads for the opt-in debug trace."""
    if not assignment.get("trace"):
        return
    if event in {"PostToolUse", "PostToolUseFailure"}:
        response = payload.get("tool_response")
        worker_event(
            assignment,
            "tool",
            tool=payload.get("tool_name"),
            tool_call_id=payload.get("tool_use_id"),
            input=payload.get("tool_input"),
            output=response
            if isinstance(response, str)
            else json.dumps(response, default=str),
            is_error=event == "PostToolUseFailure",
            error=payload.get("error"),
        )
    elif event == "PreToolUse" and _reported(assignment):
        worker_event(
            assignment,
            "tool.blocked",
            tool=payload.get("tool_name"),
            reason="report already recorded",
        )
    elif event in SESSION_EVENTS:
        worker_event(
            assignment,
            SESSION_EVENTS[event],
            session_file=payload.get("transcript_path"),
            source=payload.get("source") or payload.get("reason"),
        )


def hook_settings(
    python: str, trace: bool = False, skip_permissions: bool = False
) -> dict:
    import shlex

    def command(event: str) -> list[dict]:
        line = (
            f"{shlex.quote(python)} -m pluto_factory.claude_worker hook {event}"
        )
        return [{"hooks": [{"type": "command", "command": line}]}]

    events = (
        "SessionStart",
        "UserPromptSubmit",
        "PreToolUse",
        "Stop",
        "SessionEnd",
    )
    if trace:
        events += ("PostToolUse", "PostToolUseFailure")
    settings = {"hooks": {event: command(event) for event in events}}
    if skip_permissions:
        # Pre-accept the one-time bypass warning that would block a worker.
        settings["skipDangerousModePermissionPrompt"] = True
    return settings


def mcp_config(python: str, assignment_path: Path) -> dict:
    return {
        "mcpServers": {
            SERVER: {
                "type": "stdio",
                "command": python,
                "args": ["-m", "pluto_factory.claude_worker", "mcp"],
                "env": {"PLUTO_FACTORY_ASSIGNMENT": str(assignment_path)},
            }
        }
    }


def main(argv: list[str] | None = None) -> None:
    arguments = sys.argv[1:] if argv is None else argv
    if arguments == ["mcp"]:
        serve()
        return
    if len(arguments) == 2 and arguments[0] == "hook":
        assignment = _assignment()
        try:
            payload = json.loads(sys.stdin.read() or "{}")
        except json.JSONDecodeError:
            payload = {}
        result = handle_hook(assignment, arguments[1], payload)
        if result is not None:
            print(json.dumps(result))
        return
    raise SystemExit("usage: claude_worker mcp | hook <EventName>")


if __name__ == "__main__":
    main()
