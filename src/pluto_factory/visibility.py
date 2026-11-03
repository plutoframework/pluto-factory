"""Show the current owned worker without changing factory work state."""

import shlex
import sqlite3
import subprocess
import sys
from pathlib import Path

from .inventory import _clean, _configuration, _state


class VisibilityError(RuntimeError):
    """A current worker could not be identified or verified safely."""


def _phase_context(work: list[dict]) -> str | None:
    for eligible in (
        lambda item: (
            item["control"] not in {"paused", "deferred", "quarantined"}
            and item["phase"] != "queued"
        ),
        lambda item: (
            item["control"] in {"deferred", "quarantined"}
            and item["phase"] != "queued"
        ),
        lambda item: (
            item["control"] not in {"paused", "deferred", "quarantined"}
        ),
        lambda item: item["control"] != "paused",
        lambda item: True,
    ):
        candidates = [item for item in work if eligible(item)]
        if candidates:
            return max(candidates, key=lambda item: item["updated"])["phase"]
    return None


def current_worker(home: Path, *, work_id: str | None = None) -> dict:
    config, status = _configuration(home)
    if status != "valid":
        raise VisibilityError("Factory configuration is unavailable.")
    try:
        state = _state(home, include_sessions=True)
    except (sqlite3.Error, OSError, ValueError, RuntimeError) as error:
        raise VisibilityError(
            "Factory state could not be read safely."
        ) from error
    result = {
        "factory": config.name,
        "home": str(home.resolve()),
        "phase": None,
    }
    if state is None:
        return result | {
            "status": "idle",
            "message": "No worker has been recorded.",
        }
    if work_id is not None:
        work = [item for item in state["work"] if item["id"] == work_id]
        if not work:
            raise VisibilityError("The selected work item was not found.")
    else:
        work = [item for item in state["work"] if item["phase"] != "completed"]
    result["phase"] = _phase_context(work)
    if state["paused"]:
        return result | {
            "status": "paused",
            "message": "The factory is paused; no active worker was selected.",
        }
    candidates = [
        item
        for item in work
        if item["phase"] != "completed"
        and item["current_run"]
        and item["control"] not in {"paused", "deferred", "quarantined"}
    ]
    if len(candidates) > 1:
        raise VisibilityError(
            "Several assignments are active; inspect the factory before opening a worker."
        )
    if not candidates:
        return result | {
            "status": "idle",
            "message": "No worker session is active in the current phase.",
        }
    selected = candidates[0]
    result["phase"] = selected["phase"]
    run = state["sessions"].get(selected["current_run"])
    if not run or run["state"] not in {
        "starting",
        "running",
        "report_received",
        "settled",
    }:
        return result | {
            "status": "idle",
            "message": "The current assignment has no active worker session.",
        }
    if (
        run["work_id"] != selected["id"]
        or run["generation"] != selected["generation"]
    ):
        raise VisibilityError(
            "The current worker generation is inconsistent; no session was opened."
        )
    session = run["session"]
    if not session:
        return result | {
            "status": "starting",
            "message": "The worker is starting; its terminal is not recorded yet.",
        }
    expected = {
        "factory": config.name,
        "work_id": selected["id"],
        "assignment_id": run["id"],
        "generation": run["generation"],
        "role": run["role"],
        "owned": True,
    }
    if any(session.get(key) != value for key, value in expected.items()):
        raise VisibilityError(
            "The recorded session does not match the current assignment."
        )
    address = session.get("address")
    if (
        not isinstance(address, str)
        or not address
        or address.startswith("-")
        or any(character.isspace() for character in address)
    ):
        raise VisibilityError(
            "The current worker has no valid Herdr terminal address."
        )
    if (
        not isinstance(session.get("core_identity"), str)
        or not session["core_identity"]
    ):
        raise VisibilityError("The recorded Herdr Core identity is missing.")
    return result | {
        "status": "active",
        "terminal": address,
        "core_identity": session["core_identity"],
        **expected,
    }


def _json_operation(runner, arguments: list[str]) -> dict:
    try:
        completed = runner(
            arguments, capture_output=True, text=True, check=False, timeout=15
        )
        if completed.returncode:
            raise VisibilityError(
                "Herdr Core could not verify the current worker."
            )
        import json

        value = json.loads(completed.stdout)
    except (OSError, subprocess.TimeoutExpired, ValueError) as error:
        raise VisibilityError(
            "Herdr Core is unavailable or returned invalid metadata."
        ) from error
    if not isinstance(value, dict):
        raise VisibilityError("Herdr Core returned invalid metadata.")
    value = value.get("result", value)
    if not isinstance(value, dict) or value.get("error"):
        raise VisibilityError("Herdr Core could not verify the current worker.")
    return value


def open_worker(
    home: Path,
    *,
    terminal: bool = False,
    work_id: str | None = None,
    runner=None,
    stdin=None,
    stdout=None,
    stderr=None,
) -> dict:
    runner = runner or subprocess.run
    selected = current_worker(home, work_id=work_id)
    if selected["status"] != "active":
        return selected
    core = _json_operation(runner, ["hc", "server", "status", "--json"])
    core_metadata = core.get("core")
    if (
        not isinstance(core_metadata, dict)
        or core_metadata.get("id") != selected["core_identity"]
    ):
        raise VisibilityError(
            "Herdr Core changed since this worker was recorded; no session was opened."
        )
    observed = _json_operation(
        runner, ["hc", "get", "--json", selected["terminal"]]
    )
    if (
        observed.get("state") != "live"
        or observed.get("address") != selected["terminal"]
    ):
        return selected | {
            "status": "idle",
            "message": "The recorded worker terminal is no longer live.",
        }
    expected = {
        "factory": selected["factory"],
        "work": selected["work_id"],
        "assignment": selected["assignment_id"],
        "generation": str(selected["generation"]),
        "role": selected["role"],
    }
    labels = observed.get("labels", {})
    if not isinstance(labels, dict) or any(
        labels.get(key) != value for key, value in expected.items()
    ):
        raise VisibilityError(
            "The live terminal does not belong to the current assignment."
        )
    latest = current_worker(home, work_id=work_id)
    if any(
        latest.get(key) != selected.get(key)
        for key in (
            "status",
            "factory",
            "assignment_id",
            "work_id",
            "generation",
            "terminal",
        )
    ):
        raise VisibilityError(
            "The active worker changed during lookup; run open again."
        )
    if terminal:
        try:
            completed = runner(
                ["hc", "attach", selected["terminal"]],
                stdin=sys.stdin if stdin is None else stdin,
                stdout=sys.stdout if stdout is None else stdout,
                stderr=sys.stderr if stderr is None else stderr,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise VisibilityError(
                "The current worker terminal could not be attached."
            ) from error
        if completed.returncode:
            raise VisibilityError(
                "Attaching to the current worker did not complete successfully."
            )
        return selected | {
            "status": "attached",
            "message": "Returned from the current worker terminal.",
        }
    try:
        completed = runner(
            [
                "herdr-app",
                "do",
                "terminal.peek",
                "--terminal",
                selected["terminal"],
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
        if completed.returncode == 0:
            import json

            response = json.loads(completed.stdout or "{}")
            if isinstance(response, dict) and not response.get("error"):
                return selected | {
                    "status": "opened",
                    "message": "Opened the current worker in Herdr.",
                }
    except (OSError, ValueError, subprocess.TimeoutExpired):
        pass
    return selected | {
        "status": "app_unavailable",
        "message": "The Herdr window is unavailable.",
        "fallback": shlex.join(
            ["pluto", "factory", "open", "--home", str(home.resolve())]
            + (["--work", work_id] if work_id is not None else [])
            + ["--terminal"]
        ),
    }


def render_open(result: dict) -> str:
    phase = f" Phase: {_clean(result['phase'])}." if result.get("phase") else ""
    message = (
        _clean(result.get("message", "No worker session was opened.")) + phase
    )
    if result.get("fallback"):
        message += (
            "\nAttach through this terminal instead:\n  " + result["fallback"]
        )
    return message
