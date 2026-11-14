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
import sys
from pathlib import Path

from pydantic import ValidationError

from .config import atomic_json
from .models import WorkerReport
from .trace import worker_event

SERVER = "factory"
TOOL = "factory_report"
QUALIFIED_TOOL = f"mcp__{SERVER}__{TOOL}"
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


def handle_message(assignment: dict, message: dict) -> dict | None:
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
        result = {"tools": [_tool_definition(assignment)]}
    elif method == "tools/call":
        params = message.get("params") or {}
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
    assignment = _assignment()
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
            response = handle_message(assignment, message)
        if response is not None:
            stdout.write(json.dumps(response) + "\n")
            stdout.flush()


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
