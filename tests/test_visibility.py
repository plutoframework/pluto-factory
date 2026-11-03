"""Current-assignment selection and UI/terminal handoff stay read-only."""

import io
import json
import subprocess

import pytest

from pluto_factory.cli import main
from pluto_factory.config import write_config
from pluto_factory.models import FactoryConfig, Repository, Submission
from pluto_factory.state import State
from pluto_factory.visibility import (
    VisibilityError,
    current_worker,
    open_worker,
    render_open,
)


def factory(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    home = tmp_path / "factory"
    write_config(
        home,
        FactoryConfig(
            name="example",
            repositories=[Repository(name="app", path=str(repo))],
        ),
    )
    return home, State(home)


def worker(
    state,
    *,
    assignment="assignment-current",
    address="t-new",
    title="Current work",
):
    work = state.submit(
        Submission(
            title=title,
            instruction="Implement the increment",
            acceptance=["The result is verified"],
            repositories=["app"],
        )
    )
    run = state.create_run(
        work["id"],
        "implementer",
        {"id": assignment, "argv": ["hidden-secret-fixture"]},
    )
    session = {
        "kind": "pi",
        "factory": "example",
        "work_id": work["id"],
        "assignment_id": run["id"],
        "generation": run["generation"],
        "role": "implementer",
        "owned": True,
        "core_identity": "core-original",
        "address": address,
        "argv": ["hidden-secret-fixture"],
    }
    state.update_run(run["id"], state="running", session=session)
    state.update_work(work["id"], phase="implementing")
    return state.get_work(work["id"]), session


class Core:
    def __init__(self, session, *, app_available=True):
        self.session = session
        self.app_available = app_available
        self.calls = []
        self.identity = "core-original"
        self.labels = {
            "factory": "example",
            "work": session["work_id"],
            "assignment": session["assignment_id"],
            "generation": str(session["generation"]),
            "role": "implementer",
        }
        self.state = "live"

    def __call__(self, arguments, **options):
        self.calls.append((arguments, options))
        assert options.get("shell") is not True
        if arguments[:3] == ["hc", "server", "status"]:
            value = {"core": {"id": self.identity}}
        elif arguments[:2] == ["hc", "get"]:
            value = {
                "address": self.session["address"],
                "state": self.state,
                "labels": self.labels,
            }
        elif arguments[:2] == ["hc", "attach"]:
            return subprocess.CompletedProcess(arguments, 0)
        elif arguments[:3] == ["herdr-app", "do", "terminal.peek"]:
            if not self.app_available:
                raise FileNotFoundError("application unavailable")
            value = {}
        else:
            raise AssertionError(f"unexpected operation: {arguments}")
        return subprocess.CompletedProcess(
            arguments, 0, json.dumps({"result": value}), ""
        )


def test_open_uses_current_assignment_not_old_paused_worker(tmp_path):
    home, state = factory(tmp_path)
    old, _ = worker(
        state, assignment="assignment-old", address="t-old", title="Paused work"
    )
    state.pause(old["id"])
    current, session = worker(state)
    runner = Core(session)
    result = open_worker(home, runner=runner)
    assert result["status"] == "opened"
    assert result["work_id"] == current["id"]
    assert runner.calls[-1][0] == [
        "herdr-app",
        "do",
        "terminal.peek",
        "--terminal",
        "t-new",
    ]
    assert all("t-old" not in arguments for arguments, _ in runner.calls)
    assert "hidden-secret-fixture" not in json.dumps(result)
    assert "t-new" not in render_open(result)
    assert current["id"] not in render_open(result)
    state.close()


@pytest.mark.parametrize(
    "field", ["factory", "work", "assignment", "generation"]
)
def test_changed_live_ownership_labels_prevent_ui(tmp_path, field):
    home, state = factory(tmp_path)
    _, session = worker(state)
    runner = Core(session)
    runner.labels[field] = "different-owner"
    with pytest.raises(VisibilityError, match="does not belong"):
        open_worker(home, runner=runner)
    assert all(arguments[0] != "herdr-app" for arguments, _ in runner.calls)
    state.close()


def test_changed_core_identity_prevents_get_and_ui(tmp_path):
    home, state = factory(tmp_path)
    _, session = worker(state)
    runner = Core(session)
    runner.identity = "replacement-core"
    with pytest.raises(VisibilityError, match="Core changed"):
        open_worker(home, runner=runner)
    assert len(runner.calls) == 1
    state.close()


def test_stale_recorded_generation_is_rejected_before_core(tmp_path):
    home, state = factory(tmp_path)
    _, session = worker(state)
    session["generation"] -= 1
    state.update_run(session["assignment_id"], session=session)
    runner = Core(session)
    with pytest.raises(VisibilityError, match="does not match"):
        open_worker(home, runner=runner)
    assert not runner.calls
    state.close()


def test_terminal_mode_preserves_caller_streams_and_needs_no_app(tmp_path):
    home, state = factory(tmp_path)
    _, session = worker(state)
    runner = Core(session, app_available=False)
    streams = {name: io.StringIO() for name in ("stdin", "stdout", "stderr")}
    result = open_worker(home, terminal=True, runner=runner, **streams)
    assert result["status"] == "attached"
    arguments, options = runner.calls[-1]
    assert arguments == ["hc", "attach", "t-new"]
    assert options == streams | {"check": False}
    assert all(arguments[0] != "herdr-app" for arguments, _ in runner.calls)
    state.close()


def test_missing_app_returns_semantic_terminal_fallback(tmp_path):
    home, state = factory(tmp_path)
    _, session = worker(state)
    runner = Core(session, app_available=False)
    result = open_worker(home, runner=runner)
    assert result["status"] == "app_unavailable"
    assert "pluto factory open --home" in result["fallback"]
    assert result["fallback"].endswith(" --terminal")
    assert "t-new" not in render_open(result)
    assert all(arguments != ["herdr-app"] for arguments, _ in runner.calls)
    state.close()


def test_idle_phase_does_not_contact_core(tmp_path):
    home, state = factory(tmp_path)
    work = state.submit(
        Submission(
            title="Checks",
            instruction="Check the candidate",
            acceptance=["Checks pass"],
            repositories=["app"],
        )
    )
    state.update_work(work["id"], phase="checking")
    result = open_worker(
        home, runner=lambda *_args, **_kwargs: pytest.fail("contacted Core")
    )
    assert result["status"] == "idle"
    assert result["phase"] == "checking"
    assert "checking" in render_open(result)
    state.close()


def test_idle_context_prefers_deferred_integration_over_old_paused_work(
    tmp_path,
):
    home, state = factory(tmp_path)
    old, _ = worker(state, assignment="old-assignment", address="old-terminal")
    state.pause(old["id"])
    current = state.submit(
        Submission(
            title="Publish foundation",
            instruction="Publish the verified change",
            acceptance=["PR is published"],
            repositories=["app"],
        )
    )
    state.update_work(current["id"], phase="integrating", control="deferred")
    result = open_worker(
        home, runner=lambda *_args, **_kwargs: pytest.fail("contacted Core")
    )
    assert result["status"] == "idle"
    assert result["phase"] == "integrating"
    state.close()


def test_attach_os_error_is_a_safe_visibility_error(tmp_path):
    home, state = factory(tmp_path)
    _, session = worker(state)
    core = Core(session)

    def runner(arguments, **options):
        if arguments[:2] == ["hc", "attach"]:
            raise FileNotFoundError("hc vanished")
        return core(arguments, **options)

    with pytest.raises(VisibilityError, match="could not be attached"):
        open_worker(home, terminal=True, runner=runner)
    state.close()


def test_factory_pause_does_not_open_retained_session(tmp_path):
    home, state = factory(tmp_path)
    worker(state)
    state.pause()
    result = open_worker(
        home, runner=lambda *_args, **_kwargs: pytest.fail("contacted Core")
    )
    assert result["status"] == "paused"
    state.close()


def test_pause_during_core_lookup_prevents_ui(tmp_path):
    home, state = factory(tmp_path)
    work, session = worker(state)
    core = Core(session)

    def runner(arguments, **options):
        result = core(arguments, **options)
        if arguments[:2] == ["hc", "get"]:
            state.pause(work["id"])
        return result

    with pytest.raises(VisibilityError, match="changed during lookup"):
        open_worker(home, runner=runner)
    assert all(arguments[0] != "herdr-app" for arguments, _ in core.calls)
    state.close()


def test_snapshot_returns_only_sanitized_current_session_metadata(tmp_path):
    home, state = factory(tmp_path)
    worker(state)
    result = current_worker(home)
    assert "hidden-secret-fixture" not in json.dumps(result)
    assert "argv" not in json.dumps(result)
    state.close()


def test_cli_open_human_and_json_modes(tmp_path, monkeypatch, capsys):
    from pluto_factory import visibility

    result = {
        "status": "opened",
        "message": "Opened the current worker in Herdr.",
        "phase": "reviewing",
        "terminal": "opaque-terminal",
        "work_id": "opaque-work",
    }
    monkeypatch.setattr(
        visibility, "open_worker", lambda *_args, **_kwargs: result
    )
    assert main(["open", "--home", str(tmp_path)]) == 0
    human = capsys.readouterr().out
    assert "reviewing" in human
    assert "opaque-terminal" not in human and "opaque-work" not in human
    assert main(["--json", "open", "--home", str(tmp_path)]) == 0
    assert json.loads(capsys.readouterr().out)["terminal"] == "opaque-terminal"
