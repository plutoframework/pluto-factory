"""Termination with in-flight steps, and the exclusive coordinator lock."""

import signal
import subprocess
import sys
import textwrap
import threading
import time

import pytest
from test_parallel import build, parallel

from pluto_factory.engine import SHUTDOWN_DRAIN_SECONDS, Engine
from pluto_factory.state import State
from pluto_factory.steppool import StepPool

SCRIPT = textwrap.dedent(
    """
    import sys, threading
    from pathlib import Path
    from pluto_factory.engine import Engine
    from pluto_factory.tailnet import unwind_on_termination

    release = threading.Event()  # never set: a step stuck on a worker session

    def stuck(self, work):
        print("step-started", flush=True)
        release.wait()

    Engine._step = stuck
    engine = Engine(Path(sys.argv[1]))
    try:
        with unwind_on_termination():
            engine.run()
    except KeyboardInterrupt:
        print("interrupted", flush=True)
        sys.exit(130)
    """
)


def start(tmp_path, home):
    script = tmp_path / "coordinator.py"
    script.write_text(SCRIPT)
    process = subprocess.Popen(
        [sys.executable, str(script), str(home)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if "step-started" in process.stdout.readline():
            return process
        if process.poll() is not None:
            break
    process.kill()
    raise AssertionError(process.stderr.read())


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT])
def test_termination_with_a_stuck_step_exits_within_the_bound(tmp_path, sig):
    home, _ = build(tmp_path, ["a"], **parallel())
    process = start(tmp_path, home)
    try:
        started = time.monotonic()
        process.send_signal(sig)
        code = process.wait(timeout=SHUTDOWN_DRAIN_SECONDS + 20)
        elapsed = time.monotonic() - started
    finally:
        if process.poll() is None:
            process.kill()
    assert code == 130, process.stderr.read()
    assert elapsed < SHUTDOWN_DRAIN_SECONDS + 10
    # The lock is released only once the process is gone: restart works.
    with State(home).writer():
        pass
    events = [e["kind"] for e in State(home).inspect()["events"]]
    assert "coordinator.interrupted" in events


def test_second_coordinator_is_refused_while_the_first_is_alive(tmp_path):
    home, _ = build(tmp_path, ["a"], **parallel())
    first = start(tmp_path, home)
    try:
        with pytest.raises(RuntimeError, match="already running"):
            Engine(home).run(once=True)
        # Still refused after the failed attempt: the lock stayed exclusive.
        with (
            pytest.raises(RuntimeError, match="already running"),
            State(home).writer(),
        ):
            pass
    finally:
        first.send_signal(signal.SIGTERM)
        first.wait(timeout=SHUTDOWN_DRAIN_SECONDS + 20)
    with State(home).writer():  # free again once the first one exited
        pass


def test_step_pool_threads_are_daemons_and_shutdown_never_blocks():
    pool = StepPool(2)
    gate = threading.Event()
    running = pool.submit(gate.wait)
    time.sleep(0.05)
    queued = [pool.submit(gate.wait) for _ in range(3)]
    started = time.monotonic()
    pool.shutdown(wait=False, cancel_futures=True)
    assert time.monotonic() - started < 1
    assert all(t.daemon for t in pool._threads)
    assert any(f.cancelled() for f in queued)
    assert not running.done()  # abandoned, not joined
    with pytest.raises(RuntimeError):
        pool.submit(lambda: None)
    gate.set()
    assert running.result(timeout=5) is True


def test_step_pool_runs_and_reports_results_and_errors():
    pool = StepPool(2)
    assert pool.submit(lambda: 7).result(timeout=5) == 7

    def boom():
        raise ValueError("bad")

    with pytest.raises(ValueError):
        pool.submit(boom).result(timeout=5)
    pool.shutdown(wait=True)


def test_step_pool_gives_each_outstanding_item_a_thread():
    # Deterministic: hold the first thread in the window where it already
    # counts itself idle but has not taken its item yet, then submit again.
    import queue
    import threading

    from pluto_factory.steppool import StepPool

    pool = StepPool(2)
    waiting, release = threading.Event(), threading.Event()
    real = queue.SimpleQueue()

    class Gated:
        def put(self, item):
            real.put(item)

        def qsize(self):
            return real.qsize()

        def get(self):
            waiting.set()
            release.wait(5)
            return real.get()

    pool._queue = Gated()
    barrier = threading.Barrier(2, timeout=5)
    first = pool.submit(barrier.wait)
    assert waiting.wait(5)
    second = pool.submit(barrier.wait)
    release.set()
    first.result(timeout=10)
    second.result(timeout=10)
    pool.shutdown(wait=False)
