"""Worker terminals are closed when their run ends and reaped when orphaned."""

import shutil
import time

from test_engine import ObservedHarness
from test_harness import FakeCore

from pluto_factory import cli
from pluto_factory.demo import prepare_demo
from pluto_factory.engine import Engine
from pluto_factory.harness import FixtureHarness, HarnessError, PiHarness
from pluto_factory.models import Failure


class RecordingFixture(FixtureHarness):
    """Fixture harness that records closes and the run state at close."""

    def __init__(self, config, state):
        super().__init__(config)
        self.state = state
        self.closed = []

    def close(self, session):
        run = self.state.get_run(session["assignment_id"])
        self.closed.append((run["id"], run["state"], run["report"]))


def run_demo(tmp_path, *, keep):
    home, _ = prepare_demo(tmp_path / "demo")
    engine = Engine(home)
    engine.config.keep_finished_sessions = keep
    harness = RecordingFixture(engine.config, engine.state)
    engine.harness = harness
    try:
        snapshot = engine.run(max_seconds=300)
        settled = [
            row["id"]
            for row in engine.state.db.execute(
                "SELECT id FROM runs WHERE state='settled'"
            )
        ]
        events = [event["kind"] for event in snapshot["events"]]
    finally:
        engine.state.close()
    return snapshot, harness.closed, settled, events


def test_settled_runs_close_their_session_after_report(tmp_path):
    snapshot, closed, settled, events = run_demo(tmp_path, keep=False)
    assert all(work["phase"] == "completed" for work in snapshot["work"])
    assert settled
    assert sorted(run_id for run_id, _, _ in closed) == sorted(settled)
    # Closed only after the report was recorded and the run settled.
    assert all(state == "settled" and report for _, state, report in closed)
    assert events.count("worker.session_closed") == len(settled)


def test_keep_finished_sessions_leaves_sessions_open(tmp_path):
    snapshot, closed, settled, events = run_demo(tmp_path, keep=True)
    assert all(work["phase"] == "completed" for work in snapshot["work"])
    assert settled
    assert closed == []
    assert "worker.session_closed" not in events


def labelled(address, **labels):
    return {
        "address": address,
        "state": "live",
        "status": {"state": "done"},
        "labels": labels,
    }


def reaper_engine(tmp_path, *, keep=False):
    home, prepared = prepare_demo(tmp_path / "reaper")
    engine = Engine(home)
    engine.config.keep_finished_sessions = keep
    work_id = prepared["submitted"]["id"]
    current = engine.state.create_run(
        work_id, "implementer", {"id": "run-current"}
    )
    engine.state.update_work(work_id, current_run=None)
    stale = engine.state.create_run(work_id, "implementer", {"id": "run-old"})
    engine.state.update_run(stale["id"], state="settled")
    engine.state.update_work(work_id, current_run=current["id"])
    name = engine.config.name
    core = FakeCore(
        [
            labelled(
                "t1", factory=name, assignment="run-current", work=work_id
            ),
            labelled("t2", factory=name, assignment="run-old", work=work_id),
            labelled("t3", factory=name, assignment="run-vanished"),
            labelled("t4", factory="other", assignment="run-old"),
            labelled("t5"),
            labelled("t6", factory=name),
            {"address": "t8", "state": "live"},
        ]
    )
    engine.harness = PiHarness(engine.config, core)
    return engine, core, work_id


def closed_addresses(core):
    return {
        call[1]: sorted(c[-1] for c in core.calls if c[1] == call[1])
        for call in core.calls
        if call[1] in {"kill", "rm"}
    }


def test_reaper_closes_only_stale_owned_terminals(tmp_path):
    engine, core, work_id = reaper_engine(tmp_path)
    try:
        reaped = engine.reap_sessions()
        events = engine.state.inspect()["events"]
    finally:
        engine.state.close()
    assert sorted(item["address"] for item in reaped) == ["t2", "t3"]
    assert closed_addresses(core) == {"kill": ["t2", "t3"], "rm": ["t2", "t3"]}
    listing = next(call for call in core.calls if call[1] == "ls")
    assert f"factory={engine.config.name}" in listing
    kinds = [
        (event["kind"], event["data"]["address"])
        for event in events
        if event["kind"] == "worker.session_reaped"
    ]
    assert sorted(kinds) == [
        ("worker.session_reaped", "t2"),
        ("worker.session_reaped", "t3"),
    ]
    assert any(
        event["kind"] == "worker.session_reaped" and event["subject"] == work_id
        for event in events
    )


def test_reaper_respects_keep_finished_sessions(tmp_path):
    engine, core, _ = reaper_engine(tmp_path, keep=True)
    try:
        reaped = engine.reap_sessions()
    finally:
        engine.state.close()
    # The settled run is kept for inspection; an unknown assignment is not.
    assert [item["address"] for item in reaped] == ["t3"]
    assert closed_addresses(core) == {"kill": ["t3"], "rm": ["t3"]}


def test_reaper_skips_terminal_whose_labels_changed(tmp_path):
    engine, core, _ = reaper_engine(tmp_path)
    listed = [dict(t) for t in core.terminals]
    # Core reports different labels on re-verification: ownership changed.
    for terminal in core.terminals:
        if terminal["address"] == "t2":
            terminal["labels"] = {"factory": "other"}
    engine.harness.owned_terminals = lambda: [
        t for t in listed if t["address"] in {"t2"}
    ]
    try:
        reaped = engine.reap_sessions()
        events = [e["kind"] for e in engine.state.inspect()["events"]]
    finally:
        engine.state.close()
    assert reaped == []
    assert not any(call[1] in {"kill", "rm"} for call in core.calls)
    assert "worker.session_reap_failed" in events


def test_reaper_runs_on_coordinator_start(tmp_path, monkeypatch):
    home, _ = prepare_demo(tmp_path / "start")
    engine = Engine(home)
    calls = []

    def reap():
        engine._reaped_at = time.monotonic()
        calls.append(True)
        return []

    monkeypatch.setattr(engine, "reap_sessions", reap)
    try:
        engine.run(once=True)
    finally:
        engine.state.close()
    # Once on start; the periodic sweep waits for its interval.
    assert calls == [True]


def test_finished_terminal_is_removed_without_kill(tmp_path):
    engine, core, _ = reaper_engine(tmp_path)
    for terminal in core.terminals:
        if terminal["address"] == "t2":
            terminal["state"] = "finished"
    try:
        engine.reap_sessions()
    finally:
        engine.state.close()
    assert closed_addresses(core) == {"kill": ["t3"], "rm": ["t2", "t3"]}


def failing_run(tmp_path, harness, *, keep=False):
    home, prepared = prepare_demo(tmp_path / "failing")
    engine = Engine(home, harness=harness)
    engine.config.max_recoveries = 0
    engine.config.keep_finished_sessions = keep
    work_id = prepared["submitted"]["id"]
    run = engine.state.create_run(work_id, "implementer", {"id": "run-fail"})
    engine.state.update_run(run["id"], state="running", session={"owned": True})
    return engine, work_id, run


def test_recovery_closes_interrupted_worker_that_then_settles(tmp_path):
    harness = ObservedHarness("working")
    engine, work_id, run = failing_run(tmp_path, harness, keep=True)
    try:
        engine._failure(
            work_id,
            Failure(code="phase.timeout", summary="Late", source="coordinator"),
        )
        events = [e["kind"] for e in engine.state.inspect()["events"]]
        assert engine.state.get_run(run["id"])["state"] == "failed"
    finally:
        engine.state.close()
    assert harness.interrupted
    # Closed even though it reported settled and sessions are kept.
    assert harness.closed == [{"owned": True}]
    assert "worker.session_closed" in events


def test_recovery_close_failure_after_settle_does_not_quarantine(tmp_path):
    class StubbornHarness(ObservedHarness):
        def close(self, session):
            raise HarnessError("transport.unavailable", "Core busy")

    engine, work_id, _ = failing_run(tmp_path, StubbornHarness("working"))
    try:
        engine._failure(
            work_id,
            Failure(code="phase.timeout", summary="Late", source="coordinator"),
        )
        work = engine.state.get_work(work_id)
        events = [e["kind"] for e in engine.state.inspect()["events"]]
    finally:
        engine.state.close()
    assert work["control"] == "deferred"
    assert "worker.session_close_failed" in events


def test_failed_settled_worker_is_closed_unless_kept(tmp_path):
    failure = Failure(code="harness.error", summary="Bad", source="runtime")
    harness = ObservedHarness("settled")
    engine, work_id, _ = failing_run(tmp_path / "close", harness)
    try:
        engine._failure(work_id, failure)
    finally:
        engine.state.close()
    assert not harness.interrupted
    assert harness.closed == [{"owned": True}]

    kept = ObservedHarness("settled")
    engine, work_id, _ = failing_run(tmp_path / "keep", kept, keep=True)
    try:
        engine._failure(work_id, failure)
    finally:
        engine.state.close()
    assert kept.closed == []


def test_doctor_reports_orphaned_session_count(tmp_path, monkeypatch, capsys):
    home, _ = prepare_demo(tmp_path / "doctor")
    name = cli.load_config(home).name
    core = FakeCore(
        [
            labelled("t1", factory=name, assignment="run-gone"),
            labelled("t2", factory=name, assignment="run-older"),
            labelled("t3", factory="other", assignment="run-gone"),
            labelled("t4"),
        ]
    )
    monkeypatch.setattr(
        "pluto_factory.harness.make_harness",
        lambda values: PiHarness(values, core),
    )
    real_which = shutil.which
    monkeypatch.setattr(
        cli.shutil,
        "which",
        lambda name: "/usr/bin/hc" if name == "hc" else real_which(name),
    )
    original = cli.load_config

    def as_pi(path):
        loaded = original(path)
        loaded.harness = "pi"
        return loaded

    monkeypatch.setattr(cli, "load_config", as_pi)
    monkeypatch.setattr(
        PiHarness,
        "readiness",
        lambda self, home, role, refresh=False: {"status": "ready"},
    )
    result = cli.doctor(home)
    assert result["orphaned_sessions"] == 2
    assert not any(call[1] in {"kill", "rm"} for call in core.calls)


class LabellessListingCore(FakeCore):
    """Real `hc ls` filters by --label but omits labels from its output."""

    def __call__(self, args, **kwargs):
        if args[0] == "hc" and args[1] == "ls":
            import json
            import subprocess

            self.calls.append(args)
            wanted = args[args.index("--label") + 1].split("=", 1)
            listed = [
                {k: v for k, v in terminal.items() if k != "labels"}
                for terminal in self.terminals
                if terminal.get("labels", {}).get(wanted[0]) == wanted[1]
            ]
            return subprocess.CompletedProcess(
                args, 0, json.dumps({"terminals": listed}), ""
            )
        return super().__call__(args, **kwargs)


def test_reaper_reads_labels_when_listing_omits_them(tmp_path):
    engine, core, _work_id = reaper_engine(tmp_path)
    real = LabellessListingCore(core.terminals)
    engine.harness = PiHarness(engine.config, real)
    try:
        reaped = engine.reap_sessions()
    finally:
        engine.state.close()
    assert sorted(item["address"] for item in reaped) == ["t2", "t3"]
    assert closed_addresses(real) == {"kill": ["t2", "t3"], "rm": ["t2", "t3"]}
