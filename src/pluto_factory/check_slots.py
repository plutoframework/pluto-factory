"""Host-wide counting semaphore for check executions.

Each slot is a lock file under ``<home>/locks/checks``; a ``flock`` on an
open file description is held for the duration of a check. It works across
threads (separate descriptions) and across factory processes on one host,
and the kernel releases it if the holder dies.
"""

from __future__ import annotations

import fcntl
import os
import time
from contextlib import contextmanager
from pathlib import Path


class SlotBusy(Exception):
    """No check slot became free before the deadline."""


class CheckSlots:
    def __init__(self, home: Path, limit: int = 1, poll: float = 0.2) -> None:
        self.folder = Path(home) / "locks" / "checks"
        self.limit = max(1, limit)
        self.poll = poll

    def _try(self) -> int | None:
        self.folder.mkdir(parents=True, exist_ok=True)
        for index in range(self.limit):
            descriptor = os.open(
                self.folder / f"slot-{index}.lock",
                os.O_RDWR | os.O_CREAT,
                0o600,
            )
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return descriptor
            except OSError:
                os.close(descriptor)
        return None

    @contextmanager
    def acquire(self, timeout: float | None = None, on_wait=None):
        """Hold one slot; ``on_wait`` is called once if a wait is needed.

        Raises :class:`SlotBusy` after ``timeout`` seconds (None: wait).
        """
        deadline = None if timeout is None else time.monotonic() + timeout
        descriptor = self._try()
        if descriptor is None and on_wait:
            on_wait()
        while descriptor is None:
            if deadline is not None and time.monotonic() >= deadline:
                raise SlotBusy("check slot busy")
            time.sleep(self.poll)
            descriptor = self._try()
        try:
            yield
        finally:
            os.close(descriptor)
