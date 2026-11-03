"""SQLite work ledger, report deduplication and transactional notifications."""

import fcntl
import hashlib
import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from .dependencies import (
    canonical_existing,
    validate_graph,
)
from .dependencies import (
    dependency_status as graph_dependency_status,
)
from .dependencies import (
    source_key as dependency_source_key,
)
from .models import Submission, WorkerReport


def identity(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:16]}"


def encode(value) -> str:
    return json.dumps(value, sort_keys=True)


class StateConflict(RuntimeError):
    """An owner changed control state while an operation was in progress."""


class State:
    def __init__(self, home: Path):
        self.home = Path(home).resolve()
        self.home.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.home / "state.sqlite3", timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.execute("PRAGMA journal_mode = WAL")
        self.db.execute("PRAGMA busy_timeout = 10000")
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, 1):
            raise ValueError(f"unsupported database schema version {version}")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY, value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS work (
                id TEXT PRIMARY KEY, title TEXT NOT NULL,
                payload TEXT NOT NULL, phase TEXT NOT NULL DEFAULT 'queued',
                control TEXT NOT NULL DEFAULT 'ready',
                generation INTEGER NOT NULL DEFAULT 0,
                current_run TEXT, recoveries INTEGER NOT NULL DEFAULT 0,
                revisions INTEGER NOT NULL DEFAULT 0,
                wake_at REAL NOT NULL DEFAULT 0,
                data TEXT NOT NULL DEFAULT '{}',
                source_key TEXT UNIQUE, created REAL NOT NULL,
                updated REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS runs (
                id TEXT PRIMARY KEY, work_id TEXT NOT NULL REFERENCES work(id),
                generation INTEGER NOT NULL, role TEXT NOT NULL,
                state TEXT NOT NULL, assignment TEXT NOT NULL,
                session TEXT, report TEXT, report_digest TEXT,
                created REAL NOT NULL, updated REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS events (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                event_key TEXT UNIQUE, kind TEXT NOT NULL,
                subject TEXT NOT NULL, data TEXT NOT NULL,
                created REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS operations (
                id TEXT PRIMARY KEY, work_id TEXT NOT NULL REFERENCES work(id),
                kind TEXT NOT NULL, state TEXT NOT NULL,
                payload TEXT NOT NULL, result TEXT,
                created REAL NOT NULL, updated REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS evidence (
                id TEXT PRIMARY KEY, work_id TEXT NOT NULL REFERENCES work(id),
                run_id TEXT, data TEXT NOT NULL, created REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS notifications (
                id TEXT PRIMARY KEY, work_id TEXT,
                incident_key TEXT UNIQUE NOT NULL, kind TEXT NOT NULL,
                message TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0,
                wake_at REAL NOT NULL DEFAULT 0, last_error TEXT,
                created REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS improvements (
                id TEXT PRIMARY KEY, work_id TEXT,
                area TEXT NOT NULL, reason TEXT NOT NULL,
                content TEXT NOT NULL, status TEXT NOT NULL,
                base_digest TEXT NOT NULL, result TEXT,
                created REAL NOT NULL
            );
            PRAGMA user_version = 1;
        """)
        for folder in (
            "assignments",
            "artifacts",
            "sessions",
            "repositories",
            "worktrees",
            "revisions",
            "logs",
        ):
            (self.home / folder).mkdir(exist_ok=True)

    def close(self):
        self.db.close()

    @contextmanager
    def writer(self):
        with (self.home / "coordinator.lock").open("a+") as stream:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise RuntimeError(
                    "factory coordinator is already running"
                ) from error
            try:
                yield
            finally:
                fcntl.flock(stream, fcntl.LOCK_UN)

    @contextmanager
    def dispatch_lock(self):
        with (self.home / "dispatch.lock").open("a+") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(stream, fcntl.LOCK_UN)

    def _event(self, kind, subject, data, event_key=None):
        self.db.execute(
            "INSERT OR IGNORE INTO events "
            "(event_key, kind, subject, data, created) VALUES (?, ?, ?, ?, ?)",
            (event_key, kind, subject, encode(data), time.time()),
        )

    def event(self, kind, subject, data, event_key=None):
        with self.db:
            self._event(kind, subject, data, event_key)

    def _notify(self, work_id, kind, message, incident_key):
        self.db.execute(
            "INSERT OR IGNORE INTO notifications "
            "(id,work_id,incident_key,kind,message,created) VALUES (?,?,?,?,?,?)",
            (
                identity("notice"),
                work_id,
                incident_key,
                kind,
                message,
                time.time(),
            ),
        )

    def notify(self, work_id, kind, message, incident_key):
        with self.db:
            self._notify(work_id, kind, message, incident_key)

    def _normalize_submission(self, submission: Submission) -> Submission:
        sources = list(submission.depends_on_sources)
        if not sources and not submission.source.startswith("github:"):
            return submission
        from .config import load_config

        config = load_config(self.home)
        registered = [
            repo.github for repo in config.repositories if repo.github
        ]
        source = submission.source
        delivery = submission.delivery_key
        local_slug = None
        if source.startswith("github:"):
            own_key = dependency_source_key(f"{source}:{delivery}", registered)
            source, delivery = own_key.rsplit(":", 1)
            local_slug = source.removeprefix("github:")
        normalized = list(
            dict.fromkeys(
                dependency_source_key(value, registered, local_slug=local_slug)
                for value in sources
            )
        )
        return submission.model_copy(
            update={
                "source": source,
                "delivery_key": delivery,
                "depends_on_sources": normalized,
            }
        )

    def submit(self, submission: Submission) -> dict:
        submission = self._normalize_submission(submission)
        source_key = (
            f"{submission.source}:{submission.delivery_key}"
            if submission.delivery_key
            else None
        )
        work_id = identity("work")
        now = time.time()
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            if source_key:
                row = self.db.execute(
                    "SELECT id FROM work WHERE source_key=?", (source_key,)
                ).fetchone()
                if row is None:
                    matches = [
                        previous
                        for previous in self.db.execute(
                            "SELECT id,source_key FROM work WHERE source_key IS NOT NULL"
                        )
                        if canonical_existing(previous["source_key"])
                        == source_key
                    ]
                    if len(matches) > 1:
                        raise ValueError("ambiguous existing source identity")
                    row = matches[0] if matches else None
                if row:
                    existing = self.get_work(row["id"])
                    existing_payload = self._normalize_submission(
                        Submission.model_validate(existing["payload"])
                    ).model_dump()
                    if existing_payload != submission.model_dump():
                        raise ValueError(
                            "delivery key already identifies different work"
                        )
                    return existing
            for dependency in submission.depends_on:
                self.get_work(dependency)
            validate_graph(
                self.list_work(),
                {
                    "id": work_id,
                    "source_key": source_key,
                    "payload": submission.model_dump(),
                },
            )
            self.db.execute(
                "INSERT INTO work "
                "(id,title,payload,source_key,created,updated) VALUES (?,?,?,?,?,?)",
                (
                    work_id,
                    submission.title,
                    encode(submission.model_dump()),
                    source_key,
                    now,
                    now,
                ),
            )
            self._event("work.submitted", work_id, submission.model_dump())
        return self.get_work(work_id)

    def dependency_status(self, work_id: str) -> dict:
        """Resolve durable symbolic prerequisites without assuming intake order."""
        return graph_dependency_status(self.list_work(), work_id)

    def get_work(self, work_id: str) -> dict:
        row = self.db.execute(
            "SELECT * FROM work WHERE id=?", (work_id,)
        ).fetchone()
        if row is None:
            raise ValueError(f"unknown work item: {work_id}")
        result = dict(row)
        result["payload"] = json.loads(result["payload"])
        result["data"] = json.loads(result["data"])
        return result

    def update_work(
        self, work_id, *, event="work.updated", notice=None, **values
    ):
        allowed = {
            "phase",
            "control",
            "generation",
            "current_run",
            "recoveries",
            "revisions",
            "wake_at",
            "data",
        }
        if not values or not set(values).issubset(allowed):
            raise ValueError("invalid work update")
        stored = {
            key: encode(value) if key == "data" else value
            for key, value in values.items()
        }
        stored["updated"] = time.time()
        query = ",".join(f"{key}=?" for key in stored)
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            current = self.get_work(work_id)
            if current["control"] == "paused" or self.is_paused():
                raise StateConflict("factory or work item is paused")
            self.db.execute(
                f"UPDATE work SET {query} WHERE id=?",
                (*stored.values(), work_id),
            )
            self._event(event, work_id, values)
            if notice:
                self._notify(work_id, **notice)
        return self.get_work(work_id)

    def list_work(self) -> list[dict]:
        rows = self.db.execute(
            "SELECT id FROM work ORDER BY created"
        ).fetchall()
        return [self.get_work(row["id"]) for row in rows]

    def create_run(self, work_id: str, role: str, assignment: dict) -> dict:
        now = time.time()
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            current = self.get_work(work_id)
            if current["control"] == "paused" or self.is_paused():
                raise StateConflict("dispatch was paused")
            if current["current_run"]:
                raise StateConflict(
                    "work item already has an active assignment"
                )
            generation = current["generation"] + 1
            assignment = {**assignment, "generation": generation}
            self.db.execute(
                "INSERT INTO runs "
                "(id,work_id,generation,role,state,assignment,created,updated) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (
                    assignment["id"],
                    work_id,
                    generation,
                    role,
                    "starting",
                    encode(assignment),
                    now,
                    now,
                ),
            )
            self.db.execute(
                "UPDATE work SET current_run=?,generation=?,control='active',"
                "updated=? WHERE id=?",
                (assignment["id"], generation, now, work_id),
            )
            self._event("run.created", work_id, assignment)
        return self.get_run(assignment["id"])

    def get_run(self, run_id):
        row = self.db.execute(
            "SELECT * FROM runs WHERE id=?", (run_id,)
        ).fetchone()
        if row is None:
            raise ValueError("unknown assignment")
        result = dict(row)
        for field in ("assignment", "session", "report"):
            result[field] = json.loads(result[field]) if result[field] else None
        return result

    def update_run(self, run_id, *, state=None, session=None):
        with self.db:
            if state is not None:
                self.db.execute(
                    "UPDATE runs SET state=?,updated=? WHERE id=?",
                    (state, time.time(), run_id),
                )
            if session is not None:
                self.db.execute(
                    "UPDATE runs SET session=?,updated=? WHERE id=?",
                    (encode(session), time.time(), run_id),
                )

    def accept_report(self, run_id, envelope: dict) -> str:
        run = self.get_run(run_id)
        work = self.get_work(run["work_id"])
        report = WorkerReport.model_validate(envelope["report"])
        content = report.model_dump()
        checksum = hashlib.sha256(encode(content).encode()).hexdigest()
        if run["report_digest"]:
            if checksum != run["report_digest"]:
                raise ValueError("conflicting final report for assignment")
            return "duplicate"
        valid = (
            envelope.get("assignment_id") == run_id
            and envelope.get("generation") == run["generation"]
            and work["current_run"] == run_id
            and work["generation"] == run["generation"]
            and run["state"] in {"starting", "running", "report_received"}
        )
        if not valid:
            self.event("report.stale", run["work_id"], {"run_id": run_id})
            return "stale"
        with self.db:
            self.db.execute(
                "UPDATE runs SET report=?,report_digest=?,state=?,updated=? "
                "WHERE id=?",
                (
                    encode(content),
                    checksum,
                    "report_received",
                    time.time(),
                    run_id,
                ),
            )
            self._event(
                "report.accepted",
                run["work_id"],
                {
                    "run_id": run_id,
                    "report": content,
                },
                f"report:{run_id}",
            )
        return "accepted"

    def begin_operation(self, work_id, kind, payload, operation_id=None):
        operation_id = operation_id or identity("op")
        now = time.time()
        with self.db:
            self.db.execute(
                "INSERT OR IGNORE INTO operations VALUES (?,?,?,?,?,?,?,?)",
                (
                    operation_id,
                    work_id,
                    kind,
                    "pending",
                    encode(payload),
                    None,
                    now,
                    now,
                ),
            )
            self._event(
                "operation.intent",
                work_id,
                {
                    "id": operation_id,
                    "kind": kind,
                },
            )
        return operation_id

    def finish_operation(self, operation_id, result):
        with self.db:
            self.db.execute(
                "UPDATE operations SET state='observed',result=?,updated=? "
                "WHERE id=?",
                (encode(result), time.time(), operation_id),
            )

    def evidence(self, work_id, result, run_id=None):
        evidence_id = identity("evidence")
        with self.db:
            self.db.execute(
                "INSERT INTO evidence VALUES (?,?,?,?,?)",
                (evidence_id, work_id, run_id, encode(result), time.time()),
            )
        return evidence_id

    def is_paused(self):
        row = self.db.execute(
            "SELECT value FROM settings WHERE key='paused'"
        ).fetchone()
        return bool(row and row["value"] == "true")

    def pause(self, work_id=None):
        with self.db:
            if work_id:
                self.get_work(work_id)
                self.db.execute(
                    "UPDATE work SET control='paused',updated=? WHERE id=?",
                    (time.time(), work_id),
                )
            else:
                self.db.execute(
                    "INSERT OR REPLACE INTO settings VALUES ('paused','true')"
                )
            self._event("factory.paused", work_id or "factory", {})
        return {"paused": True, "work_id": work_id}

    def resume(self, work_id=None):
        with self.db:
            if work_id:
                self.get_work(work_id)
                self.db.execute(
                    "UPDATE work SET control='ready',wake_at=0,updated=? WHERE id=?",
                    (time.time(), work_id),
                )
            else:
                self.db.execute("DELETE FROM settings WHERE key='paused'")
            self._event("factory.resumed", work_id or "factory", {})
        return {"paused": False, "work_id": work_id}

    def inspect(self):
        runs = self.db.execute(
            "SELECT id FROM runs ORDER BY created"
        ).fetchall()
        return {
            "schema_version": 1,
            "paused": self.is_paused(),
            "work": self.list_work(),
            "runs": [self.get_run(row["id"]) for row in runs],
            "events": [
                dict(row) | {"data": json.loads(row["data"])}
                for row in self.db.execute("SELECT * FROM events")
            ],
            "evidence": [
                dict(row) | {"data": json.loads(row["data"])}
                for row in self.db.execute("SELECT * FROM evidence")
            ],
            "notifications": [
                dict(row)
                for row in self.db.execute("SELECT * FROM notifications")
            ],
            "improvements": [
                dict(row)
                for row in self.db.execute("SELECT * FROM improvements")
            ],
        }
