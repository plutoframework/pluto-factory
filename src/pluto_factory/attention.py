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
    created REAL NOT NULL, answered_at REAL, applied_at REAL,
    details TEXT NOT NULL DEFAULT '[]', why TEXT NOT NULL DEFAULT '',
    advice TEXT NOT NULL DEFAULT 'none', block_key TEXT,
    answered_by TEXT, custom INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS external_issues (
    key TEXT PRIMARY KEY, slug TEXT NOT NULL, number INTEGER NOT NULL,
    state TEXT NOT NULL DEFAULT 'open', closing TEXT NOT NULL DEFAULT '',
    checked REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS upstream_issues (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_id TEXT NOT NULL, library TEXT NOT NULL,
    slug TEXT NOT NULL, number INTEGER, url TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
    wake_at REAL NOT NULL DEFAULT 0, last_error TEXT,
    created REAL NOT NULL, UNIQUE (decision_id, library)
);
CREATE TABLE IF NOT EXISTS triage_issues (
    slug TEXT NOT NULL, number INTEGER NOT NULL,
    version TEXT NOT NULL, updated_at TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL, spec TEXT, guidance TEXT NOT NULL DEFAULT '',
    author TEXT NOT NULL DEFAULT '', trusted INTEGER NOT NULL DEFAULT 0,
    decision_id TEXT, created REAL NOT NULL, updated REAL NOT NULL,
    PRIMARY KEY (slug, number)
);
CREATE TABLE IF NOT EXISTS issue_comments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_id TEXT NOT NULL, kind TEXT NOT NULL,
    slug TEXT NOT NULL, issue INTEGER NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
    wake_at REAL NOT NULL DEFAULT 0, last_error TEXT,
    created REAL NOT NULL, UNIQUE (decision_id, kind)
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

DECISION_KINDS = {"choice", "free_text", "config", "framework_gap"}
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


DECISION_COLUMNS = {
    "details": "TEXT NOT NULL DEFAULT '[]'",
    "why": "TEXT NOT NULL DEFAULT ''",
    "advice": "TEXT NOT NULL DEFAULT 'none'",
    "block_key": "TEXT",
    "answered_by": "TEXT",
    "custom": "INTEGER NOT NULL DEFAULT 0",
    "auto": "TEXT",
}


def ensure_schema(db):
    db.executescript(SCHEMA)
    present = {row[1] for row in db.execute("PRAGMA table_info(decisions)")}
    for column, definition in DECISION_COLUMNS.items():
        if column not in present:
            db.execute(
                f"ALTER TABLE decisions ADD COLUMN {column} {definition}"
            )


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
    result["details"] = json.loads(result.get("details") or "[]")
    result["auto"] = json.loads(result["auto"]) if result.get("auto") else None
    return result


def _upstream_field(payload):
    if not isinstance(payload, dict):
        return {}
    return {
        "upstream": {
            "title": _clip(payload.get("title"), 200),
            "problem": _clip(payload.get("problem"), 2000),
            "repro": _clip(payload.get("repro"), 2000),
            "fix": _clip(payload.get("fix"), 2000),
            "acceptance": [
                _clip(item, 300) for item in payload.get("acceptance") or []
            ][:8],
        }
    }


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
                "effect": _clip(option.get("effect"), 300),
                "locus": _clip(option.get("locus") or "app", 200),
                "fix": _clip(option.get("fix"), 300),
                **_upstream_field(option.get("upstream")),
                "tradeoff": _clip(option.get("tradeoff"), 300),
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
    details=None,
    why="",
    advice="none",
    block_key=None,
):
    """File a decision for the owner; the same open work+question is one.

    ``block_key`` also dedupes: the advisor may reword the question later, so
    a repeated block is recognised by its raw reason, not its headline.
    ``advice='pending'`` queues the advisor to rewrite the headline,
    options and rationale in place.
    """
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
    if kind in {"choice", "framework_gap"} and len(options) < 2:
        raise AttentionError("a choice needs at least two options")
    with state.db:
        state.db.execute("BEGIN IMMEDIATE")
        if work_id is not None:
            state.get_work(work_id)
        existing = state.db.execute(
            "SELECT * FROM decisions WHERE work_id IS ? AND "
            "(question=? OR (block_key IS NOT NULL AND block_key=?)) "
            "AND state IN ('open','answered','auto_pending')",
            (work_id, question, block_key),
        ).fetchone()
        if existing:
            return _decision(existing)
        decision_id = _id("decision")
        state.db.execute(
            "INSERT INTO decisions "
            "(id,work_id,kind,question,context,options,proposal,created,"
            "details,why,advice,block_key) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                decision_id,
                work_id,
                kind,
                question,
                _clip(context, CONTEXT_LIMIT),
                json.dumps(options),
                json.dumps(proposal) if proposal else None,
                time.time(),
                json.dumps([_clip(d, 300) for d in (details or [])][:8]),
                _clip(why, 300),
                advice,
                block_key,
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
    decision = get_decision(state, decision_id)
    if advice == "none":
        enqueue_issue_comment(state, decision, "created")
    return decision


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


def answer_decision(state, decision_id, answer, note="", via="cli", by="owner"):
    """Record the owner's answer only; the coordinator applies it."""
    answer = _clip(answer, ANSWER_LIMIT).strip()
    note = _clip(note, ANSWER_LIMIT).strip()
    if not answer:
        raise AttentionError("answer is empty")
    with state.db:
        state.db.execute("BEGIN IMMEDIATE")
        decision = get_decision(state, decision_id)
        auto = decision.get("auto") or {}
        if (
            decision["state"] == "applied"
            and auto.get("status") == "applied"
            and by != AUTO_BY
        ):
            return _override_applied(state, decision, answer, note, by)
        if decision["state"] not in {"open", "auto_pending"}:
            raise DecisionConflict(f"decision is already {decision['state']}")
        if decision["state"] == "auto_pending" and by != AUTO_BY:
            # Owner answered inside the grace window: their answer wins.
            auto["status"] = "overridden"
            state.db.execute(
                "UPDATE decisions SET auto=? WHERE id=?",
                (json.dumps(auto), decision_id),
            )
        labels = [option["label"] for option in decision["options"]]
        # Config changes stay options-only; every other kind takes free text.
        if decision["kind"] == "config" and answer not in labels:
            raise AttentionError("answer must be one of the listed options")
        custom = int(answer not in labels)
        state.db.execute(
            "UPDATE decisions SET state='answered',answer=?,note=?,"
            "answered_at=?,answered_by=?,custom=? WHERE id=?",
            (
                answer,
                note,
                time.time(),
                _clip(by, 200) or "owner",
                custom,
                decision_id,
            ),
        )
        state._event(
            "decision.answered",
            decision["work_id"] or "factory",
            {
                "id": decision_id,
                "answer": answer[:200],
                "custom": bool(custom),
                "via": via,
                "by": _clip(by, 200) or "owner",
            },
        )
    return get_decision(state, decision_id)


AUTO_BY = "advisor (auto)"


def _override_applied(state, decision, answer, note, by):
    """Owner overrides an automatic decision that already took effect."""
    auto = {**decision["auto"], "status": "overridden"}
    text = (
        f"Owner override of automatic decision '{decision['question']}': "
        + (answer + (f". Note: {note}" if note else ""))
    )
    with state.db:
        state.db.execute(
            "UPDATE decisions SET auto=?,answer=?,note=?,custom=1,"
            "answered_by=? WHERE id=?",
            (
                json.dumps(auto),
                answer,
                note,
                _clip(by, 200) or "owner",
                decision["id"],
            ),
        )
        state._event(
            "decision.overridden",
            decision["work_id"] or "factory",
            {"id": decision["id"], "answer": answer[:200]},
        )
    if decision["work_id"]:
        work = state.get_work(decision["work_id"])
        state.add_guidance(decision["work_id"], text, "owner")
        if work["control"] in {"deferred", "quarantined", "backoff"}:
            state.resume(decision["work_id"])
    return get_decision(state, decision["id"])


def auto_verdict(policy, advice, option_of=None, packet=None):
    """Deterministic gate: (ok, why-not). The model never overrides it."""
    from .advisor import AUTO_CATEGORIES

    if policy.mode != "auto_obvious":
        return False, "autonomy mode is ask_all"
    if advice.autonomy != "auto":
        return False, "advisor asked the owner"
    if advice.category not in AUTO_CATEGORIES:
        return False, f"category {advice.category} is owner-only"
    if advice.confidence != "high":
        return False, "confidence below high"
    if not advice.reversible:
        return False, "not reversible"
    chosen = [o for o in advice.options if o.recommended]
    if len(chosen) != 1:
        return False, "no single recommended option"
    option = chosen[0]
    framework = option.locus.startswith("framework:")
    if advice.category == "upstream_issue" and not framework:
        return False, "upstream category needs a framework option"
    if advice.category in {"app_fix", "config_change"} and framework:
        return False, "category conflicts with a framework option"
    if advice.category == "resume_after_fix" and not (packet or {}).get(
        "fix_verified"
    ):
        return False, "framework fix is not verified by the factory"
    if advice.category == "config_change":
        change = advice.config_change
        bound = policy.max_config.get(change.key)
        if (
            change.key not in CONFIG_KEYS
            or bound is None
            or change.value < 0
            or change.value > bound
        ):
            return False, "config change outside max_config"
    return True, ""


def set_auto_pending(state, decision_id, advice, grace_seconds):
    """Hold the advisor's recommended answer for the grace window."""
    option = next(o for o in advice.options if o.recommended)
    answer = option.label
    kind, proposal, options = None, None, None
    if advice.category == "config_change":
        change = advice.config_change
        proposal = validate_proposal({"key": change.key, "value": change.value})
        kind, answer = "config", "Apply"
        options = normalize_options(
            [{"label": "Apply", "recommended": True}, {"label": "Decline"}]
        )
    auto = {
        "category": advice.category,
        "reason": advice.autonomy_reason,
        "confidence": advice.confidence,
        "answer": answer,
        "apply_at": time.time() + grace_seconds,
        "status": "pending",
    }
    with state.db:
        if kind:
            state.db.execute(
                "UPDATE decisions SET kind=?,proposal=?,options=? WHERE id=?",
                (kind, json.dumps(proposal), json.dumps(options), decision_id),
            )
        state.db.execute(
            "UPDATE decisions SET state='auto_pending',auto=? "
            "WHERE id=? AND state='open'",
            (json.dumps(auto), decision_id),
        )
        state._event(
            "decision.auto_pending",
            get_decision(state, decision_id)["work_id"] or "factory",
            {"id": decision_id, "category": advice.category, "answer": answer},
        )
    decision = get_decision(state, decision_id)
    enqueue_issue_comment(state, decision, "auto")
    return decision


def apply_due_autos(state, now=None):
    """Answer automatic decisions whose grace window has expired."""
    now = time.time() if now is None else now
    done = []
    for decision in list_decisions(state, {"auto_pending"}):
        auto = decision["auto"] or {}
        if auto.get("apply_at", 0) > now:
            continue
        auto["status"] = "applied"
        with state.db:
            state.db.execute(
                "UPDATE decisions SET state='open',auto=? WHERE id=? "
                "AND state='auto_pending'",
                (json.dumps(auto), decision["id"]),
            )
        answer_decision(
            state,
            decision["id"],
            auto["answer"],
            "",
            via="auto",
            by=AUTO_BY,
        )
        done.append(decision["id"])
    return done


def withdraw_decision(state, decision_id, note=""):
    with state.db:
        decision = get_decision(state, decision_id)
        if decision["state"] not in {"open", "auto_pending", "answered"}:
            raise DecisionConflict(f"decision is already {decision['state']}")
        state.db.execute(
            "UPDATE decisions SET state='withdrawn',note=? WHERE id=?",
            (_clip(note, ANSWER_LIMIT).strip(), decision_id),
        )
        state._event(
            "decision.withdrawn",
            decision["work_id"] or "factory",
            {"id": decision_id, "note": _clip(note, 300)},
        )
    return get_decision(state, decision_id)


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


def chosen_option(decision):
    for option in decision["options"]:
        if option["label"] == decision["answer"]:
            return option
    return None


def framework_library(option):
    locus = (option or {}).get("locus", "app")
    return locus.partition(":")[2] if locus.startswith("framework:") else ""


GAP_RESUME = "Framework fixed; resume"
GAP_WITHDRAW = "Withdraw; handle in the app"


def record_framework_gap(state, decision, option, pins):
    """File a framework-gap item for a chosen framework option.

    The blocked work stays parked: only an answer to the gap decision (or a
    bump of the library's pinned version) resumes it. No framework repo is
    edited.
    """
    library = framework_library(option)
    gap = add_decision(
        state,
        work_id=decision["work_id"],
        kind="framework_gap",
        question=f"Framework gap in {library}: {option['fix']}"[:500],
        context=(
            f"Chosen: {option['label']}.\nBlocked: {decision['question']}\n"
            f"Pinned: {library} {pins.get(library, 'unknown')}"
        ),
        options=[
            {"label": GAP_RESUME, "recommended": True},
            {"label": GAP_WITHDRAW},
        ],
        proposal={"library": library, "pin": pins.get(library, "unknown")},
        block_key=f"gap:{decision['id']}",
        notify=True,
    )
    state.event(
        "framework_gap.recorded",
        decision["work_id"] or "factory",
        {"id": gap["id"], "library": library, "fix": option["fix"][:300]},
    )
    return gap


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


def set_pr(ledger, row, **fields):
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
    with ledger.db:
        ledger.db.execute(
            f"UPDATE pull_requests SET {assignments} WHERE id=?",
            (*stored.values(), row["id"]),
        )
        if new != previous:
            subject = row["work_id"] or "factory"
            ledger._event(
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
                ledger._notify(
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
                "('open','answered','auto_pending') OR (state='applied' AND applied_at>?) "
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


def apply_advice(state, decision_id, advice, source, announce=True):
    """Replace a pending decision's headline/options with advisor output."""
    options = normalize_options(
        [
            {
                "label": o.label,
                "effect": o.effect,
                "tradeoff": o.tradeoff,
                "locus": o.locus,
                "fix": o.fix,
                "upstream": o.upstream.model_dump() if o.upstream else None,
                "description": o.effect,
                "recommended": o.recommended,
            }
            for o in advice.options
        ]
    )
    with state.db:
        changed = state.db.execute(
            "UPDATE decisions SET question=?,details=?,options=?,why=?,"
            "advice=? WHERE id=? AND state='open' AND advice='pending'",
            (
                _clip(advice.problem, QUESTION_LIMIT),
                json.dumps(advice.details),
                json.dumps(options),
                advice.why,
                source,
                decision_id,
            ),
        ).rowcount
        if not changed:
            state.db.execute(
                "UPDATE decisions SET advice='skipped' WHERE id=? "
                "AND advice='pending'",
                (decision_id,),
            )
        else:
            state._event(
                "decision.advised",
                get_decision(state, decision_id)["work_id"] or "factory",
                {"id": decision_id, "source": source},
            )
    if changed and announce:
        enqueue_issue_comment(
            state, get_decision(state, decision_id), "created"
        )
    return bool(changed)


# --- GitHub issue history (coordinator-side outbox) ------------------------

MARKER = "<!-- pluto-factory:decision:{id}:{kind} -->"
ISSUE_SOURCE = re.compile(r"^github:([^:]+):(\d+)$")


def marker(decision_id, kind):
    return MARKER.format(id=decision_id, kind=kind)


def source_issue(state, work_id):
    """(slug, issue number) of the work's source issue, if it has one."""
    if not work_id:
        return None
    from .dependencies import canonical_existing

    row = state.db.execute(
        "SELECT source_key FROM work WHERE id=?", (work_id,)
    ).fetchone()
    match = (
        ISSUE_SOURCE.fullmatch(canonical_existing(row[0]) or "")
        if row
        else None
    )
    return (match[1], int(match[2])) if match else None


def enqueue_issue_comment(state, decision, kind):
    """Queue one comment per (decision, kind); a no-op without an issue."""
    issue = source_issue(state, decision["work_id"])
    if issue is None:
        return False
    with state.db:
        cursor = state.db.execute(
            "INSERT OR IGNORE INTO issue_comments "
            "(decision_id,kind,slug,issue,created) VALUES (?,?,?,?,?)",
            (decision["id"], kind, issue[0], issue[1], time.time()),
        )
    return bool(cursor.rowcount)


def _words(text, limit):
    return (
        text if len(text.split()) <= limit else " ".join(text.split()[:limit])
    )


def comment_body(decision, kind, extra=None):
    lines = [marker(decision["id"], kind)]
    if kind == "upstream":
        lines.append(
            f"**Upstream issue filed** for {extra['library']}: {extra['url']}"
        )
        lines.append(
            "Work stays parked until it closes; the factory resumes it then."
        )
    elif kind == "auto":
        auto = decision.get("auto") or {}
        lines.append(f"**Decided automatically:** {decision['question']}")
        lines.append(f"- Answer: {auto.get('answer', '')}")
        lines.append(f"- Reason: {auto.get('reason', '')}")
        lines.append(
            "- Applies after a grace period unless the owner overrides it."
        )
    elif kind == "created":
        lines.append(f"**Owner decision needed:** {decision['question']}")
        for detail in decision["details"]:
            lines.append(f"- {detail}")
        lines.append("")
        for option in decision["options"]:
            star = " (recommended)" if option["recommended"] else ""
            effect = option.get("effect") or option.get("description") or ""
            tradeoff = option.get("tradeoff")
            line = f"- **{option['label']}**{star}: {effect}"
            lines.append(line + (f" Tradeoff: {tradeoff}" if tradeoff else ""))
        if decision.get("why"):
            lines.append(f"\nRecommended because: {decision['why']}")
    else:
        stamp = time.strftime(
            "%Y-%m-%d %H:%M:%SZ", time.gmtime(decision["answered_at"] or 0)
        )
        lines.append(f"**Decision answered:** {decision['question']}")
        lines.append(
            f"- Answer: {decision['answer']}"
            + (" (custom answer)" if decision.get("custom") else "")
        )
        if decision.get("note"):
            lines.append(f"- Custom feedback: {decision['note']}")
        lines.append(f"- By: {decision.get('answered_by') or 'owner'}")
        lines.append(f"- At: {stamp}")
    return _clip("\n".join(lines), 6000)


def flush_issue_comments(state, adapter, allowed_slugs):
    """Post queued comments once each; retry with backoff on failure."""
    from .github import GitHubFailure

    posted = 0
    rows = state.db.execute(
        "SELECT * FROM issue_comments WHERE state='pending' AND wake_at<=? "
        "ORDER BY id",
        (time.time(),),
    ).fetchall()
    for row in rows:
        decision = get_decision(state, row["decision_id"])
        if row["slug"] not in allowed_slugs:
            outcome, error = "skipped", "issue repository is not registered"
        else:
            try:
                existing = adapter._json(
                    [
                        "gh",
                        "issue",
                        "view",
                        str(row["issue"]),
                        "--repo",
                        row["slug"],
                        "--json",
                        "comments",
                    ]
                )
                tag = marker(row["decision_id"], row["kind"])
                if any(
                    tag in (c.get("body") or "")
                    for c in existing.get("comments") or []
                ):
                    outcome, error = "delivered", None  # already there
                else:
                    extra = None
                    if row["kind"] == "upstream":
                        extra = dict(
                            state.db.execute(
                                "SELECT library,url FROM upstream_issues "
                                "WHERE decision_id=? AND url!='' LIMIT 1",
                                (row["decision_id"],),
                            ).fetchone()
                        )
                    adapter._run(
                        [
                            "gh",
                            "issue",
                            "comment",
                            str(row["issue"]),
                            "--repo",
                            row["slug"],
                            "--body",
                            comment_body(decision, row["kind"], extra),
                        ],
                        mutating=True,
                    )
                    outcome, error = "delivered", None
                    posted += 1
            except GitHubFailure as failure:
                outcome, error = "retry", failure.code
        with state.db:
            if outcome == "retry":
                state.db.execute(
                    "UPDATE issue_comments SET attempts=attempts+1,"
                    "last_error=?,wake_at=? WHERE id=?",
                    (
                        error,
                        time.time() + min(5 * 2 ** row["attempts"], 300),
                        row["id"],
                    ),
                )
            else:
                state.db.execute(
                    "UPDATE issue_comments SET state=?,attempts=attempts+1,"
                    "last_error=? WHERE id=?",
                    (outcome, error, row["id"]),
                )
    return posted
