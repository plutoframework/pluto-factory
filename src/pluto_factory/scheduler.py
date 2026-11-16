"""Admission policy for parallel work: who may start or step right now.

Pure decisions over a snapshot of the ledger. The engine asks ``admit`` for
every ready item in FIFO order (dependency order is already enforced by
readiness) and steps those admitted. Items already holding a worker slot
(mid-pipeline) always continue unless a per-phase cap holds them; only new
starts compete for workers, repositories, paths and host headroom.
"""

import os
import re
import time
from dataclasses import dataclass

# Phases in which an item holds a worker slot between its start and its
# delivery. ``awaiting_merge`` only polls GitHub and never holds one.
PIPELINE = frozenset(
    {
        "implementing",
        "checking",
        "reviewing",
        "preparing",
        "qa_checks",
        "qa",
        "pushing",
        "integrating",
    }
)
PARKED = frozenset({"paused", "deferred", "quarantined"})

_EXTENSIONS = (
    "py|go|ts|tsx|js|jsx|mjs|rs|md|toml|json|yaml|yml|sql|css|html|sh|"
    "proto|tmpl|mod|sum|lock"
)
_PATH = re.compile(
    r"(?<![\w/.@:-])"
    r"((?:\.{0,2}/)?(?:[\w.-]+/)+[\w.*-]*|[\w-]+\.(?:" + _EXTENSIONS + r"))"
    r"(?![\w-])"
)
_STAT = re.compile(r"^\s*(\S.*?)\s+\|\s+\d+", re.MULTILINE)


def _normalize(path: str) -> str:
    path = path.strip().rstrip(".,;:)`'\"")
    while path.startswith("./"):
        path = path[2:]
    return path.strip("/")


def estimate_paths(work: dict) -> frozenset[str]:
    """Paths this item will likely touch; empty means unknown.

    Sources, in order of trust: the file lists of earlier runs (diff stat of
    recorded candidates, worker artifacts and finding locations) and path
    tokens in the accepted spec (instruction and acceptance criteria).
    """
    found: set[str] = set()
    data = work.get("data") or {}
    for entry in data.get("entries", []):
        for match in _STAT.finditer(entry.get("diff_stat") or ""):
            name = match.group(1)
            if "=>" not in name and "..." not in name:
                found.add(_normalize(name))
    found.update(_normalize(p) for p in data.get("touched_paths", []))
    for report in (data.get("reports") or {}).values():
        found.update(_normalize(p) for p in report.get("artifacts", []))
        for finding in report.get("findings", []):
            location = (finding.get("location") or "").split(":")[0]
            if location:
                found.add(_normalize(location))
    payload = work.get("payload") or {}
    text = "\n".join(
        [
            payload.get("instruction", ""),
            *payload.get("acceptance", []),
            payload.get("title", ""),
        ]
    )
    found.update(_normalize(m.group(1)) for m in _PATH.finditer(text))
    return frozenset(path for path in found if path and "*" not in path)


def paths_overlap(left, right) -> bool:
    """True when any path equals, contains or lies inside another."""
    for a in left:
        for b in right:
            if a == b or a.startswith(b + "/") or b.startswith(a + "/"):
                return True
    return False


def read_free_memory_mb() -> float | None:
    try:
        with open("/proc/meminfo") as stream:
            for line in stream:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) / 1024
    except (OSError, ValueError, IndexError):
        pass
    return None


def read_load_average() -> float | None:
    try:
        return os.getloadavg()[0]
    except (OSError, AttributeError):
        return None


@dataclass
class Admission:
    admitted: bool
    reason: str = ""


class Scheduler:
    def __init__(
        self,
        config,
        *,
        clock=time.monotonic,
        free_memory=read_free_memory_mb,
        load_average=read_load_average,
    ):
        self.config = config
        self.clock = clock
        self.free_memory = free_memory
        self.load_average = load_average
        self._last_start: float | None = None

    @property
    def policy(self):
        return self.config.concurrency

    def note_start(self) -> None:
        self._last_start = self.clock()

    # -- helpers ---------------------------------------------------------

    def _write_repositories(self, work) -> set[str]:
        access = {repo.name: repo.access for repo in self.config.repositories}
        names = work["payload"].get("repositories", [])
        writable = {n for n in names if access.get(n, "write") == "write"}
        return writable or set(names)

    @staticmethod
    def holds_slot(work, running) -> bool:
        """Mid-pipeline items and items whose start is already in flight."""
        if work["phase"] == "completed" or work["control"] in PARKED:
            return False
        return work["phase"] in PIPELINE or running.get(work["id"]) == "queued"

    def active(self, work_items, running) -> list[dict]:
        return [w for w in work_items if self.holds_slot(w, running)]

    def _host_pressure(self) -> str:
        policy = self.policy
        if policy.min_free_memory_mb is not None:
            free = self.free_memory()
            if free is not None and free < policy.min_free_memory_mb:
                return f"free memory {free:.0f} MB below minimum"
        if policy.max_load_average is not None:
            load = self.load_average()
            if load is not None and load > policy.max_load_average:
                return f"load average {load:.1f} above maximum"
        return ""

    def _phase_full(self, phase, work, work_items, running) -> bool:
        cap = self.policy.max_per_phase.get(phase)
        if cap is None:
            return False
        busy = {
            w["id"]
            for w in work_items
            if w["id"] != work["id"]
            and w["phase"] == phase
            and (w["current_run"] or running.get(w["id"]) == phase)
        }
        busy |= {
            wid
            for wid, ph in running.items()
            if ph == phase and wid != work["id"]
        }
        return len(busy) >= cap

    # -- admission -------------------------------------------------------

    def admit(self, work, work_items, running) -> Admission:
        """May this ready item with no live run be stepped now?

        ``running`` maps work id to the phase of the step in flight.
        """
        policy = self.policy
        phase = work["phase"]
        if phase not in PIPELINE and phase != "queued":
            return Admission(True)
        if phase in PIPELINE:
            if self._phase_full(phase, work, work_items, running):
                return Admission(False, f"phase {phase} at its limit")
            return Admission(True)
        # A new start competes for every limit.
        others = [
            w for w in self.active(work_items, running) if w["id"] != work["id"]
        ]
        if len(others) >= policy.max_workers:
            return Admission(False, "all workers busy")
        mine = self._write_repositories(work)
        for name in mine:
            using = [w for w in others if name in self._write_repositories(w)]
            if len(using) >= policy.repository_limit(name):
                return Admission(False, f"repository {name} at its limit")
        mode = policy.overlap
        if mode != "none":
            estimate = estimate_paths(work)
            for other in others:
                if not mine & self._write_repositories(other):
                    continue
                if mode == "paths":
                    theirs = estimate_paths(other)
                    if (
                        estimate
                        and theirs
                        and not paths_overlap(estimate, theirs)
                    ):
                        continue
                return Admission(False, f"overlaps {other['id']}")
        if self._phase_full("implementing", work, work_items, running):
            return Admission(False, "phase implementing at its limit")
        pressure = self._host_pressure()
        if pressure:
            return Admission(False, pressure)
        if (
            self._last_start is not None
            and self.clock() - self._last_start < policy.start_stagger_seconds
        ):
            return Admission(False, "staggering starts")
        return Admission(True)
