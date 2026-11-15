"""Durable owner attention: tracked pull requests and open decisions.

Both tables live in each factory's state database. Everything here takes a
``State`` (or any object with ``db``, ``_event`` and ``_notify``) so the CLI,
the coordinator and the dashboard's single answer route share one write path.
"""

import hashlib
import json
import os
import re
import sqlite3
import time
import tomllib
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS decisions (
    id TEXT PRIMARY KEY, work_id TEXT, kind TEXT NOT NULL,
    question TEXT NOT NULL, context TEXT NOT NULL DEFAULT '',
    options TEXT NOT NULL DEFAULT '[]', proposal TEXT,
    state TEXT NOT NULL DEFAULT 'open', answer TEXT, note TEXT,
    created REAL NOT NULL, answered_at REAL, applied_at REAL
);
CREATE TABLE IF NOT EXISTS pull_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    slug TEXT NOT NULL, number INTEGER,
    title TEXT NOT NULL DEFAULT '', url TEXT NOT NULL DEFAULT '',
    branch TEXT NOT NULL DEFAULT '', base TEXT NOT NULL DEFAULT '',
    head TEXT NOT NULL DEFAULT '', origin TEXT NOT NULL,
    work_id TEXT, repo_name TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL DEFAULT 'open', reason TEXT NOT NULL DEFAULT '',
    failing TEXT NOT NULL DEFAULT '[]', log_excerpt TEXT NOT NULL DEFAULT '',
    repairs TEXT NOT NULL DEFAULT '{}', repair_total INTEGER NOT NULL DEFAULT 0,
    repair_work TEXT, merge_state TEXT NOT NULL DEFAULT '',
    review_decision TEXT NOT NULL DEFAULT '',
    state_since REAL NOT NULL, created REAL NOT NULL, updated REAL NOT NULL,
    UNIQUE (slug, number)
);
"""

DECISION_KINDS = {"choice", "free_text", "config"}
OPEN_PR_STATES = {
    "open",
    "pending",
    "failed",
    "repairing",
    "green",
    "ready",
    "needs_owner",
}
OWNER_PR_STATES = {"green", "ready", "needs_owner"}
TERMINAL_PR_STATES = {"merged", "closed", "untracked"}
QUESTION_LIMIT = 500
CONTEXT_LIMIT = 6000
ANSWER_LIMIT = 4000
APPROVALS = {"apply", "approve", "approved", "yes", "accept"}
# Numeric budgets the owner may change through a config decision.
CONFIG_KEYS = {
    "phase_timeout_seconds": int,
    "implementer_phase_timeout_seconds": int,
    "command_timeout_seconds": int,
    "max_recoveries": int,
    "max_revisions": int,
    "github_poll_seconds": int,
    "github_max_resyncs": int,
    "pr_poll_seconds": int,
    "pr_max_repairs": int,
    "pr_max_repairs_total": int,
    "retry_base_seconds": float,
}


class AttentionError(ValueError):
    pass


class UnknownDecision(KeyError):
    pass


class DecisionConflict(RuntimeError):
    pass


class TrackingRequired(ValueError):
    """A delivery step was attempted without a tracker row for its PR."""


def ensure_schema(db):
    db.executescript(SCHEMA)


def _id(prefix):
    return f"{prefix}-{hashlib.sha256(os.urandom(16)).hexdigest()[:12]}"


def _clip(value, limit):
    value = str(value or "")
    return value if len(value) <= limit else value[:limit] + " [truncated]"


# --- decisions ----------------------------------------------------------


def _decision(row):
    if row is None:
        return None
    result = dict(row)
    result["options"] = json.loads(result["options"] or "[]")
    result["proposal"] = (
        json.loads(result["proposal"]) if result["proposal"] else None
    )
    return result


def normalize_options(options):
    result = []
    for option in options or []:
        if isinstance(option, str):
            option = {"label": option}
        label = _clip(option.get("label"), 200).strip()
        if not label:
            raise AttentionError("decision option needs a label")
        result.append(
            {
                "label": label,
                "description": _clip(option.get("description"), 500),
                "recommended": bool(option.get("recommended")),
            }
        )
    if len({item["label"] for item in result}) != len(result):
        raise AttentionError("decision option labels must be unique")
    if sum(item["recommended"] for item in result) > 1:
        raise AttentionError("only one option may be recommended")
    return result


def validate_proposal(proposal):
    if not isinstance(proposal, dict):
        raise AttentionError("config decision needs a proposal")
    key, value = proposal.get("key"), proposal.get("value")
    if key not in CONFIG_KEYS:
        raise AttentionError(
            f"config key {key!r} cannot be changed by a decision; allowed: "
            + ", ".join(sorted(CONFIG_KEYS))
        )
    kind = CONFIG_KEYS[key]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise AttentionError(f"{key} must be a number")
    if kind is int and value != int(value):
        raise AttentionError(f"{key} must be a whole number")
    if value < 0:
        raise AttentionError(f"{key} must not be negative")
    return {"key": key, "value": kind(value)}


def add_decision(
    state,
    *,
    question,
    context="",
    options=None,
    kind="free_text",
    work_id=None,
    proposal=None,
    notify=True,
):
    """File a decision for the owner; the same open work+question is one."""
    if kind not in DECISION_KINDS:
        raise AttentionError(f"unknown decision kind {kind!r}")
    question = _clip(question, QUESTION_LIMIT).strip()
    if not question:
        raise AttentionError("decision question is empty")
    options = normalize_options(options)
    if kind == "config":
        proposal = validate_proposal(proposal)
        if not options:
            options = normalize_options(
                [
                    {"label": "Apply", "recommended": True},
                    {"label": "Decline"},
                ]
            )
    if kind == "choice" and len(options) < 2:
        raise AttentionError("a choice needs at least two options")
    with state.db:
        state.db.execute("BEGIN IMMEDIATE")
        if work_id is not None:
            state.get_work(work_id)
        existing = state.db.execute(
            "SELECT * FROM decisions WHERE work_id IS ? AND question=? "
            "AND state IN ('open','answered')",
            (work_id, question),
        ).fetchone()
        if existing:
            return _decision(existing)
        decision_id = _id("decision")
        state.db.execute(
            "INSERT INTO decisions "
            "(id,work_id,kind,question,context,options,proposal,created) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (
                decision_id,
                work_id,
                kind,
                question,
                _clip(context, CONTEXT_LIMIT),
                json.dumps(options),
                json.dumps(proposal) if proposal else None,
                time.time(),
            ),
        )
        state._event(
            "decision.created",
            work_id or "factory",
            {"id": decision_id, "question": question, "kind": kind},
        )
        if notify:
            state._notify(
                work_id,
                "decision",
                f"Needs your decision: {question}",
                f"decision:{decision_id}",
            )
    return get_decision(state, decision_id)


def get_decision(state, decision_id):
    row = state.db.execute(
        "SELECT * FROM decisions WHERE id=?", (decision_id,)
    ).fetchone()
    if row is None:
        raise UnknownDecision(decision_id)
    return _decision(row)


def list_decisions(state, states=None):
    rows = state.db.execute(
        "SELECT * FROM decisions ORDER BY created, id"
    ).fetchall()
    return [
        item
        for item in map(_decision, rows)
        if states is None or item["state"] in states
    ]


def answer_decision(state, decision_id, answer, note="", via="cli"):
    """Record the owner's answer only; the coordinator applies it."""
    answer = _clip(answer, ANSWER_LIMIT).strip()
    note = _clip(note, ANSWER_LIMIT).strip()
    if not answer:
        raise AttentionError("answer is empty")
    with state.db:
        state.db.execute("BEGIN IMMEDIATE")
        decision = get_decision(state, decision_id)
        if decision["state"] != "open":
            raise DecisionConflict(f"decision is already {decision['state']}")
        labels = [option["label"] for option in decision["options"]]
        if decision["kind"] in {"choice", "config"} and answer not in labels:
            raise AttentionError("answer must be one of the listed options")
        state.db.execute(
            "UPDATE decisions SET state='answered',answer=?,note=?,"
            "answered_at=? WHERE id=?",
            (answer, note, time.time(), decision_id),
        )
        state._event(
            "decision.answered",
            decision["work_id"] or "factory",
            {"id": decision_id, "answer": answer[:200], "via": via},
        )
    return get_decision(state, decision_id)


def withdraw_decision(state, decision_id):
    with state.db:
        decision = get_decision(state, decision_id)
        if decision["state"] != "open":
            raise DecisionConflict(f"decision is already {decision['state']}")
        state.db.execute(
            "UPDATE decisions SET state='withdrawn' WHERE id=?",
            (decision_id,),
        )
        state._event(
            "decision.withdrawn",
            decision["work_id"] or "factory",
            {"id": decision_id},
        )


def mark_applied(state, decision_id, detail=None):
    with state.db:
        state.db.execute(
            "UPDATE decisions SET state='applied',applied_at=? WHERE id=?",
            (time.time(), decision_id),
        )
        decision = get_decision(state, decision_id)
        state._event(
            "decision.applied",
            decision["work_id"] or "factory",
            {"id": decision_id, **(detail or {})},
        )


def guidance_text(decision):
    text = f"Owner decision on '{decision['question']}': {decision['answer']}"
    if decision.get("note"):
        text += f". Note: {decision['note']}"
    return text


def approved(decision):
    return (decision["answer"] or "").strip().lower() in APPROVALS


def apply_config_change(home, key, value):
    """Set one top-level key in factory.toml: validated, atomic, backed up."""
    from .config import load_config
    from .models import FactoryConfig

    proposal = validate_proposal({"key": key, "value": value})
    path = Path(home) / "factory.toml"
    original = path.read_text()
    rendered = f"{key} = {json.dumps(proposal['value'])}"
    lines = original.splitlines()
    pattern = re.compile(rf"^\s*{re.escape(key)}\s*=")
    table = next(
        (i for i, line in enumerate(lines) if line.lstrip().startswith("[")),
        len(lines),
    )
    index = next(
        (i for i in range(table) if pattern.match(lines[i])),
        None,
    )
    if index is None:
        lines.insert(table, rendered)
        if table < len(lines) - 1 and lines[table + 1].strip():
            lines.insert(table + 1, "")
    else:
        lines[index] = rendered
    updated = "\n".join(lines) + "\n"
    # Validate before touching the live file.
    FactoryConfig.model_validate(tomllib.loads(updated))
    backup = path.with_name(f"factory.toml.bak-{int(time.time() * 1000)}")
    backup.write_text(original)
    temporary = path.with_name(f".factory.toml.{os.getpid()}.tmp")
    temporary.write_text(updated)
    temporary.replace(path)
    try:
        load_config(Path(home))
    except Exception:
        path.write_text(original)
        raise
    return str(backup)


# --- pull requests ------------------------------------------------------


def _pr(row):
    if row is None:
        return None
    result = dict(row)
    result["failing"] = json.loads(result["failing"] or "[]")
    result["repairs"] = json.loads(result["repairs"] or "{}")
    return result


def get_pr(state, slug, number):
    return _pr(
        state.db.execute(
            "SELECT * FROM pull_requests WHERE slug=? AND number=?",
            (slug, number),
        ).fetchone()
    )


def list_prs(state, states=None):
    rows = state.db.execute(
        "SELECT * FROM pull_requests ORDER BY created, id"
    ).fetchall()
    return [
        item
        for item in map(_pr, rows)
        if states is None or item["state"] in states
    ]


def track_pr(
    state,
    slug,
    number,
    *,
    origin,
    work_id=None,
    repo_name="",
    title="",
    url="",
    branch="",
    base="",
    head="",
    event="pr.tracked",
):
    """Insert or refresh a tracker row; an untracked row stays untracked."""
    now = time.time()
    with state.db:
        row = state.db.execute(
            "SELECT * FROM pull_requests WHERE slug=? AND number=?",
            (slug, number),
        ).fetchone()
        if row is None:
            state.db.execute(
                "INSERT INTO pull_requests (slug,number,title,url,branch,"
                "base,head,origin,work_id,repo_name,state_since,created,"
                "updated) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    slug,
                    number,
                    title,
                    url,
                    branch,
                    base,
                    head,
                    origin,
                    work_id,
                    repo_name,
                    now,
                    now,
                    now,
                ),
            )
            state._event(
                event,
                work_id or "factory",
                {"slug": slug, "number": number, "origin": origin},
            )
        else:
            fields = {
                "title": title,
                "url": url,
                "branch": branch,
                "base": base,
                "head": head,
                "work_id": work_id,
                "repo_name": repo_name,
            }
            changed = {
                key: value
                for key, value in fields.items()
                if value and row[key] != value
            }
            if changed:
                assignments = ",".join(f"{key}=?" for key in changed)
                state.db.execute(
                    f"UPDATE pull_requests SET {assignments},updated=? "
                    "WHERE id=?",
                    (*changed.values(), now, row["id"]),
                )
    return get_pr(state, slug, number)


def record_intent(
    state, slug, branch, base, head, work_id, repo_name, title=""
):
    """Write the tracker row BEFORE the PR exists; the number comes later."""
    now = time.time()
    with state.db:
        row = state.db.execute(
            "SELECT id FROM pull_requests WHERE slug=? AND branch=? AND "
            "work_id IS ? AND state NOT IN ('merged','closed')",
            (slug, branch, work_id),
        ).fetchone()
        if row:
            state.db.execute(
                "UPDATE pull_requests SET head=?,updated=? WHERE id=?",
                (head, now, row["id"]),
            )
            return row["id"]
        cursor = state.db.execute(
            "INSERT INTO pull_requests (slug,title,branch,base,head,origin,"
            "work_id,repo_name,state_since,created,updated) "
            "VALUES (?,?,?,?,?,'factory',?,?,?,?,?)",
            (
                slug,
                title,
                branch,
                base,
                head,
                work_id,
                repo_name,
                now,
                now,
                now,
            ),
        )
        state._event(
            "pr.intent_recorded",
            work_id or "factory",
            {"slug": slug, "branch": branch},
        )
        return cursor.lastrowid


def fill_number(state, intent_id, number, url, head=None):
    """Complete an intent row; if the number is already tracked, merge."""
    with state.db:
        row = state.db.execute(
            "SELECT * FROM pull_requests WHERE id=?", (intent_id,)
        ).fetchone()
        twin = state.db.execute(
            "SELECT id FROM pull_requests WHERE slug=? AND number=? AND id!=?",
            (row["slug"], number, intent_id),
        ).fetchone()
        if twin:
            state.db.execute(
                "DELETE FROM pull_requests WHERE id=?", (intent_id,)
            )
            return get_pr(state, row["slug"], number)
        state.db.execute(
            "UPDATE pull_requests SET number=?,url=?,head=?,updated=? "
            "WHERE id=?",
            (number, url, head or row["head"], time.time(), intent_id),
        )
        state._event(
            "pr.opened",
            row["work_id"] or "factory",
            {"slug": row["slug"], "number": number},
        )
    return get_pr(state, row["slug"], number)


def pending_intents(state):
    return [
        dict(row)
        for row in state.db.execute(
            "SELECT * FROM pull_requests WHERE number IS NULL"
        )
    ]


def untrack(state, slug, number):
    row = get_pr(state, slug, number)
    if row is None:
        raise AttentionError("pull request is not tracked")
    if row["origin"] == "factory":
        raise AttentionError("factory-opened pull requests are always tracked")
    set_pr(state, row, state="untracked", reason="Untracked by the owner")
    return get_pr(state, slug, number)


def set_pr(state, row, **fields):
    """Update a row; a state change is one event (and maybe a notice)."""
    now = time.time()
    stored = {
        key: json.dumps(value) if key in {"failing", "repairs"} else value
        for key, value in fields.items()
    }
    previous = row["state"]
    new = fields.get("state", previous)
    if new != previous:
        stored["state_since"] = now
    stored["updated"] = now
    assignments = ",".join(f"{key}=?" for key in stored)
    with state.db:
        state.db.execute(
            f"UPDATE pull_requests SET {assignments} WHERE id=?",
            (*stored.values(), row["id"]),
        )
        if new != previous:
            subject = row["work_id"] or "factory"
            state._event(
                "pr.state_changed",
                subject,
                {
                    "slug": row["slug"],
                    "number": row["number"],
                    "from": previous,
                    "to": new,
                    "reason": _clip(fields.get("reason", row["reason"]), 500),
                    "head": fields.get("head", row["head"]),
                },
            )
            if new in {"ready", "needs_owner"}:
                head = fields.get("head", row["head"])[:8]
                reason = fields.get("reason") or row["reason"]
                label = "ready to merge" if new == "ready" else reason
                state._notify(
                    row["work_id"],
                    "attention",
                    f"{row['slug']}#{row['number']}: {label}",
                    f"pr:{row['slug']}#{row['number']}:{new}:{head}",
                )


def ensure_tracked(state, pull_requests):
    """The delivery gate: every PR of a work item must have a tracker row."""
    missing = []
    for pr in pull_requests or []:
        slug, number = pr.get("slug"), pr.get("number")
        if (
            not isinstance(slug, str)
            or not isinstance(number, int)
            or get_pr(state, slug, number) is None
        ):
            missing.append(pr.get("url") or f"{slug}#{number}")
    if missing:
        raise TrackingRequired(
            "delivery requires a pull request tracker row for: "
            + ", ".join(map(str, missing))
        )


# --- read side (also used on read-only snapshot connections) --------------


def read_attention(connection):
    """Open decisions and tracked PRs from any sqlite3 connection."""
    try:
        decisions = [
            _decision(row)
            for row in connection.execute(
                "SELECT * FROM decisions WHERE state IN "
                "('open','answered') OR (state='applied' AND applied_at>?) "
                "ORDER BY created LIMIT 200",
                (time.time() - 86400,),
            )
        ]
        prs = [
            _pr(row)
            for row in connection.execute(
                "SELECT * FROM pull_requests WHERE state NOT IN "
                "('merged','closed','untracked') AND number IS NOT NULL "
                "ORDER BY created LIMIT 300"
            )
        ]
    except (
        sqlite3.OperationalError
    ):  # tables absent: factory predates attention
        return {"decisions": [], "pull_requests": []}
    return {"decisions": decisions, "pull_requests": prs}
