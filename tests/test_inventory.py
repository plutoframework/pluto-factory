"""Inventory observes existing state without migration, writes or PID guesses."""

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

from pluto_factory.cli import main
from pluto_factory.config import write_config
from pluto_factory.inventory import (
    describe_factory,
    list_factories,
    render_inventory,
)
from pluto_factory.models import FactoryConfig, Repository, Submission
from pluto_factory.state import State


def registered(root: Path, name: str) -> Path:
    home = root / name
    write_config(
        home,
        FactoryConfig(
            name=name,
            harness="fixture",
            coordinator="rules",
            repositories=[
                Repository(name="app", path=str(root / "offline-repo"))
            ],
        ),
    )
    return home


def submit(state: State, title: str = "Example work") -> dict:
    return state.submit(
        Submission(
            title=title,
            instruction="Implement the assigned increment",
            acceptance=["Observable result is verified"],
            repositories=["app"],
        )
    )


def files(home: Path) -> dict:
    return {
        str(path.relative_to(home)): hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
        for path in home.rglob("*")
        if path.is_file()
    }


def test_registered_config_without_database_is_not_initialized(tmp_path):
    home = registered(tmp_path / "factories", "registered")
    before = files(home)
    entry = describe_factory(home)
    assert entry["name"] == "registered"
    assert entry["status"] == "registered"
    assert entry["coordinator_running"] is False
    assert entry["state_status"] == "missing"
    assert entry["repositories"][0]["path"].endswith("offline-repo")
    assert entry["models"]["coordinator"] == "rules"
    assert entry["models"]["implementer"]["model"] == "gpt-6.1-sol"
    assert entry["last_activity"]
    assert files(home) == before
    assert not (home / "state.sqlite3").exists()
    assert not (home / "coordinator.lock").exists()


def test_missing_registry_is_not_created(tmp_path):
    root = tmp_path / "absent"
    result = list_factories(root)
    assert result["factories"] == []
    assert result["registry_exists"] is False
    assert not root.exists()


def test_stopped_wal_database_is_read_without_creating_sidecars(tmp_path):
    home = registered(tmp_path / "factories", "stopped")
    state = State(home)
    work = submit(state)
    state.update_work(work["id"], phase="reviewing", control="ready")
    state.close()
    before = files(home)
    entry = describe_factory(home)
    assert entry["status"] == "stopped"
    assert entry["current_phase"] == "reviewing"
    assert entry["unfinished_count"] == 1
    assert entry["last_activity_source"] == "ledger"
    assert files(home) == before


def test_uncheckpointed_wal_updates_are_visible_without_source_writes(tmp_path):
    home = registered(tmp_path / "factories", "active-state")
    state = State(home)
    work = submit(state)
    state.update_work(work["id"], phase="qa", control="ready")
    assert (home / "state.sqlite3-wal").exists()
    before = files(home)
    entry = describe_factory(home)
    assert entry["current_phase"] == "qa"
    assert files(home) == before
    state.close()


def test_live_lock_is_detected_without_interrupting_owner(tmp_path):
    home = registered(tmp_path / "factories", "running")
    script = (
        "import fcntl,sys; "
        "stream=open(sys.argv[1],'a+'); "
        "fcntl.flock(stream,fcntl.LOCK_EX); "
        "print('ready',flush=True); sys.stdin.readline()"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", script, str(home / "coordinator.lock")],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert process.stdout.readline().strip() == "ready"
        entry = describe_factory(home)
        assert entry["status"] == "running"
        assert entry["coordinator_running"] is True
        assert entry["current_phase"] == "idle"
        assert process.poll() is None
        assert describe_factory(home)["coordinator_running"] is True
    finally:
        process.communicate("stop\n", timeout=5)
    assert describe_factory(home)["coordinator_running"] is False


def test_unlocked_stale_pid_does_not_claim_running(tmp_path):
    home = registered(tmp_path / "factories", "stale")
    (home / "coordinator.lock").write_text(str(os.getpid()))
    entry = describe_factory(home)
    assert entry["coordinator_running"] is False
    assert entry["status"] == "registered"
    assert (home / "coordinator.lock").read_text() == str(os.getpid())


def test_paused_and_deferred_statuses_follow_ledger(tmp_path):
    paused = registered(tmp_path / "factories", "paused")
    state = State(paused)
    submit(state)
    state.pause()
    state.close()
    assert describe_factory(paused)["status"] == "paused"
    deferred = registered(tmp_path / "factories", "deferred")
    state = State(deferred)
    work = submit(state)
    state.update_work(work["id"], phase="implementing", control="deferred")
    state.close()
    entry = describe_factory(deferred)
    assert entry["status"] == "deferred"
    assert entry["deferred_count"] == 1
    assert entry["current_phase"] == "implementing"


def test_broken_config_and_database_do_not_hide_other_factories(tmp_path):
    root = tmp_path / "factories"
    registered(root, "good")
    bad_config = registered(root, "bad-config")
    (bad_config / "factory.toml").write_text("invalid = [")
    bad_database = registered(root, "bad-database")
    (bad_database / "state.sqlite3").write_bytes(b"not a database")
    unrelated = root / "not-a-factory"
    unrelated.mkdir()
    result = list_factories(root)
    entries = {item["name"]: item for item in result["factories"]}
    assert set(entries) == {"good", "bad-config", "bad-database"}
    assert entries["good"]["status"] == "registered"
    assert entries["bad-config"]["configuration_status"] == "invalid"
    assert entries["bad-database"]["state_status"] == "unavailable"
    assert entries["bad-config"]["errors"]
    assert entries["bad-database"]["errors"]


def test_cli_uses_registry_and_deduplicates_explicit_home(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    root = tmp_path / "xdg" / "pluto" / "factories"
    home = registered(root, "example")
    assert main(["list", "--home", str(home), "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["root"] == str(root)
    assert len(result["factories"]) == 1


def test_current_phase_ignores_deferred_old_assignment(tmp_path):
    home = registered(tmp_path / "factories", "mixed")
    state = State(home)
    older = submit(state, "Deferred old work")
    state.update_work(older["id"], phase="implementing", control="deferred")
    current = submit(state, "Current review")
    state.update_work(current["id"], phase="reviewing", control="ready")
    state.close()
    entry = describe_factory(home)
    assert entry["current_phase"] == "reviewing"
    assert entry["current_work"]["title"] == "Current review"


def test_cli_list_defaults_to_human_text(tmp_path, capsys):
    root = tmp_path / "factories"
    home = registered(root, "example")
    state = State(home)
    work = submit(state)
    state.update_work(work["id"], phase="reviewing", control="ready")
    state.close()
    assert main(["list", "--root", str(root)]) == 0
    output = capsys.readouterr().out
    assert "Factories (1)" in output
    assert "NAME" in output and "STATUS" in output
    assert "example" in output and "reviewing" in output
    assert "Models:" in output and "gpt-6.1-sol" in output
    assert "Path:" in output and "Activity:" in output
    assert work["id"] not in output
    assert not output.lstrip().startswith("{")


def test_global_json_flag_preserves_machine_output(tmp_path, capsys):
    root = tmp_path / "factories"
    registered(root, "example")
    assert main(["--json", "list", "--root", str(root)]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["factories"][0]["name"] == "example"


def test_narrow_human_view_wraps_details_and_errors(tmp_path):
    home = registered(tmp_path / "factories", "example")
    (home / "state.sqlite3").write_bytes(b"not a database")
    output = render_inventory(list_factories(home.parent), width=44)
    assert "Status:" in output
    assert "Warning:" in output
    assert "Models:" in output
    assert all(len(line) <= 44 for line in output.splitlines())


def test_empty_inventory_has_a_readable_result(tmp_path):
    output = render_inventory(list_factories(tmp_path / "missing"), width=80)
    assert "Factories (0)" in output
    assert "No factories registered." in output
