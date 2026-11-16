"""Host-wide check semaphore, queued factory_check, per-run environment."""

import json
import multiprocessing
import sys
import threading
import time
from pathlib import Path

import pytest
from test_git import workspace
from test_harness import assignment

from pluto_factory import claude_worker
from pluto_factory.check_slots import CheckSlots, SlotBusy
from pluto_factory.git import GitFailure
from pluto_factory.models import FactoryConfig, Repository


def test_semaphore_bounds_concurrent_threads(tmp_path):
    slots = CheckSlots(tmp_path, limit=2, poll=0.01)
    live, peak, guard = 0, 0, threading.Lock()

    def work():
        nonlocal live, peak
        with slots.acquire():
            with guard:
                live += 1
                peak = max(peak, live)
            time.sleep(0.05)
            with guard:
                live -= 1

    threads = [threading.Thread(target=work) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert peak == 2


def _hold(home, ready, release):
    with CheckSlots(Path(home), 1).acquire():
        ready.set()
        release.wait(10)


def test_semaphore_spans_processes(tmp_path):
    ready, release = multiprocessing.Event(), multiprocessing.Event()
    process = multiprocessing.Process(
        target=_hold, args=(str(tmp_path), ready, release)
    )
    process.start()
    try:
        assert ready.wait(10)
        waited = []
        slots = CheckSlots(tmp_path, 1, poll=0.01)
        with (
            pytest.raises(SlotBusy),
            slots.acquire(0.2, lambda: waited.append(1)),
        ):
            pass
        assert waited == [1]
    finally:
        release.set()
        process.join(10)
    with CheckSlots(tmp_path, 1).acquire(0.5):
        pass  # released with the holder


def test_default_is_one_slot():
    config = FactoryConfig(
        name="t", repositories=[Repository(name="a", path="/tmp")]
    )
    assert config.concurrency.max_parallel_checks == 1


def test_validate_waits_for_slot_and_reports(tmp_path):
    manager, _ = workspace(tmp_path)
    entries = manager.prepare("item", ["source"])
    events = []
    with manager.check_slots.acquire(), pytest.raises(GitFailure) as error:
        manager.check_worktree(
            entries,
            slot_timeout=0.2,
            on_wait=lambda: events.append("waiting"),
            on_run=lambda: events.append("running"),
        )
    assert error.value.code == "check.slot_busy"
    assert events == ["waiting"]
    rows = manager.check_worktree(
        entries,
        on_wait=lambda: events.append("w2"),
        on_run=lambda: events.append("r2"),
    )
    assert rows[0]["passed"] and events == ["waiting", "r2"]


def test_per_run_env_applied_and_serial_default_unchanged(tmp_path):
    probe = (
        "import os,sys;"
        "print(os.environ.get('GOLANGCI_LINT_CACHE','-'),"
        "os.path.isdir(os.environ.get('GOLANGCI_LINT_CACHE','/nope')))"
    )

    def run(tmp, **extra):
        tmp.mkdir()
        manager, _ = workspace(tmp, **extra)
        manager.repositories["source"].checks[:] = [
            [sys.executable, "-c", probe]
        ]
        entries = manager.prepare("item", ["source"])
        return manager.check_worktree(entries)[0]["stdout"].split()

    serial = run(tmp_path / "s")
    assert serial == ["-", "False"]
    parallel = run(tmp_path / "p", concurrency={"max_workers": 2})
    assert parallel[1] == "True" and "check-runs" in parallel[0]
    custom = run(
        tmp_path / "c",
        checks={"per_run_env": {"GOLANGCI_LINT_CACHE": "{run_dir}/x"}},
    )
    assert custom[0].endswith("/x") and custom[1] == "True"
    assert not any((tmp_path / "c" / "factory" / "check-runs").iterdir())


def test_factory_check_reports_waiting_and_does_not_count_wait(tmp_path):
    item = assignment(tmp_path)
    item["config"] = {"command_timeout_seconds": 1}  # wait budget 64s
    notes = []
    box = {}
    thread = threading.Thread(
        target=lambda: box.update(
            r=claude_worker.request_check(item, notes.append)
        )
    )
    thread.start()
    for _ in range(100):
        if claude_worker.pending_checks(item):
            break
        time.sleep(0.05)
    request = claude_worker.pending_checks(item)[0]
    claude_worker.check_status(item, request, "waiting")
    time.sleep(1.2)
    assert thread.is_alive() and "waiting for check slot" in notes
    claude_worker.check_status(item, request, "running")
    claude_worker.answer_check(item, request, {"passed": True})
    thread.join(5)
    assert box["r"] == {"passed": True}


def test_duplicate_factory_check_returns_in_flight_result(tmp_path):
    item = assignment(tmp_path)
    replies = []
    threads = [
        threading.Thread(
            target=lambda: replies.append(claude_worker.request_check(item))
        )
        for _ in range(2)
    ]
    threads[0].start()
    for _ in range(100):
        if claude_worker.pending_checks(item):
            break
        time.sleep(0.05)
    threads[1].start()
    time.sleep(0.6)
    folder = Path(item["assignment_path"]).parent / "checks"
    assert len(list(folder.glob("*.request.json"))) == 1
    claude_worker.answer_check(
        item, claude_worker.pending_checks(item)[0], {"passed": True, "n": 1}
    )
    [t.join(5) for t in threads]
    assert replies == [{"passed": True, "n": 1}] * 2


def test_report_not_blocked_by_inflight_check(tmp_path):
    import io

    from test_claude_worker import report

    item = assignment(tmp_path)
    Path(item["assignment_path"]).write_text(json.dumps(item))
    messages = [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "factory_check", "arguments": {}},
        },
    ]
    read, write = __import__("os").pipe()
    reader = __import__("os").fdopen(read)
    writer = __import__("os").fdopen(write, "w")
    stdout = io.StringIO()
    import os

    os.environ["PLUTO_FACTORY_ASSIGNMENT"] = item["assignment_path"]
    try:
        server = threading.Thread(
            target=claude_worker.serve, args=(reader, stdout), daemon=True
        )
        server.start()
        writer.write(json.dumps(messages[0]) + "\n")
        writer.flush()
        for _ in range(100):
            if claude_worker.pending_checks(item):
                break
            time.sleep(0.05)
        writer.write(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {"name": "factory_report", "arguments": report()},
                }
            )
            + "\n"
        )
        writer.flush()
        for _ in range(100):
            if '"id": 2' in stdout.getvalue():
                break
            time.sleep(0.05)
        assert '"id": 2' in stdout.getvalue()
        assert '"id": 1' not in stdout.getvalue()
        assert Path(item["report_path"]).exists()
    finally:
        os.environ.pop("PLUTO_FACTORY_ASSIGNMENT", None)
        claude_worker.answer_check(
            item, claude_worker.pending_checks(item)[0], {"passed": True}
        )
        writer.close()
        server.join(15)
