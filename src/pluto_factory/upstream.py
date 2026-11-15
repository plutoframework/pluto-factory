"""Cross-factory work through GitHub issues only (no factory-to-factory RPC).

A chosen framework option files an issue in the mapped upstream repository
(the label its factory's intake watches), links it on the source issue, and
makes the blocked work wait on it. External dependencies are then polled on
the GitHub cadence: closed-as-completed resumes the work, not-planned asks
the owner.
"""

import re
import time

from . import attention
from .advisor import terse
from .github import GitHubFailure

BODY_EVIDENCE_LIMIT = 1500
SLUG = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
ISSUE_URL = re.compile(r"https://github\.com/([^/]+/[^/]+)/issues/(\d+)")


def upstream_marker(factory, decision_id):
    return f"<!-- pluto-factory:upstream:{factory}:{decision_id} -->"


def request_upstream(state, config, decision, option):
    """Queue (idempotently) an upstream issue for a framework option."""
    library = attention.framework_library(option)
    repo = config.framework.repository(library)
    if not repo:
        state.event(
            "upstream.unmapped",
            decision["work_id"] or "factory",
            {"library": library, "hint": "add [framework].upstream"},
            f"upstream-unmapped:{decision['id']}:{library}",
        )
        return False
    with state.db:
        cursor = state.db.execute(
            "INSERT OR IGNORE INTO upstream_issues "
            "(decision_id,library,slug,created) VALUES (?,?,?,?)",
            (decision["id"], library, repo, time.time()),
        )
    return bool(cursor.rowcount)


def _body(state, config, decision, option, library):
    from .dashboard_data import text as redact

    work = state.get_work(decision["work_id"]) if decision["work_id"] else None
    lines = [
        upstream_marker(config.name, decision["id"]),
        f"**Problem:** {terse(decision['question'])}",
        "",
        f"**Library:** {library}",
        f"**Proposed fix:** {option.get('fix') or option['label']}",
    ]
    if option.get("effect"):
        lines.append(f"**Effect:** {option['effect']}")
    evidence = redact(decision["context"], BODY_EVIDENCE_LIMIT).strip()
    if evidence:
        lines += [
            "",
            "**Evidence (bounded, redacted):**",
            "```",
            evidence,
            "```",
        ]
    issue = attention.source_issue(state, decision["work_id"])
    lines.append("")
    if issue:
        lines.append(
            f"**Origin:** https://github.com/{issue[0]}/issues/{issue[1]}"
        )
    lines.append(
        f"**Factory:** {config.name}"
        + (f", work item {work['id']}" if work else "")
    )
    return "\n".join(lines)


def _find_existing(adapter, repo, label, tag):
    """Adopt an issue created just before a crash (marker in its body)."""
    rows = adapter._json(
        [
            "gh",
            "issue",
            "list",
            "--repo",
            repo,
            "--state",
            "all",
            "--label",
            label,
            "--limit",
            "100",
            "--json",
            "number,url,body",
        ]
    )
    for row in rows:
        if tag in (row.get("body") or ""):
            return row["number"], row["url"]
    return None


def flush_upstream(state, adapter, config):
    """File queued upstream issues; link them and park the work on them."""
    filed = 0
    rows = state.db.execute(
        "SELECT * FROM upstream_issues WHERE state='pending' AND wake_at<=? "
        "ORDER BY id",
        (time.time(),),
    ).fetchall()
    for row in rows:
        decision = attention.get_decision(state, row["decision_id"])
        option = next(
            (
                o
                for o in decision["options"]
                if attention.framework_library(o).lower()
                == row["library"].lower()
            ),
            None,
        )
        try:
            if option is None:
                raise ValueError("framework option no longer present")
            tag = upstream_marker(config.name, decision["id"])
            label = config.framework.proposed_label
            found = _find_existing(adapter, row["slug"], label, tag)
            if found is None:
                adapter._run(
                    [
                        "gh",
                        "label",
                        "create",
                        label,
                        "--repo",
                        row["slug"],
                        "--description",
                        "Proposed by a factory; the owner decides if ready",
                        "--force",
                    ],
                    mutating=True,
                    acceptable=(0, 1),
                )
                result = adapter._run(
                    [
                        "gh",
                        "issue",
                        "create",
                        "--repo",
                        row["slug"],
                        "--title",
                        f"[{config.name}] {terse(decision['question'], 14)}"[
                            :200
                        ],
                        "--body",
                        _body(state, config, decision, option, row["library"]),
                        "--label",
                        label,
                    ],
                    mutating=True,
                )
                match = ISSUE_URL.search(result.stdout or "")
                if not match:
                    raise GitHubFailure(
                        "github.invalid_response",
                        "issue creation output unreadable",
                        True,
                    )
                found = (int(match[2]), match[0])
                filed += 1
            number, url = found
        except (GitHubFailure, ValueError) as error:
            code = getattr(error, "code", "upstream.invalid")
            with state.db:
                state.db.execute(
                    "UPDATE upstream_issues SET attempts=attempts+1,"
                    "last_error=?,wake_at=? WHERE id=?",
                    (
                        code,
                        time.time() + min(5 * 2 ** row["attempts"], 300),
                        row["id"],
                    ),
                )
            continue
        key = f"github:{row['slug'].lower()}:{number}"
        with state.db:
            state.db.execute(
                "UPDATE upstream_issues SET number=?,url=?,state='filed' "
                "WHERE id=?",
                (number, url, row["id"]),
            )
            state.db.execute(
                "INSERT OR IGNORE INTO external_issues (key,slug,number) "
                "VALUES (?,?,?)",
                (key, row["slug"].lower(), number),
            )
            state._event(
                "upstream.filed",
                decision["work_id"] or "factory",
                {"decision": decision["id"], "issue": key, "url": url},
            )
        if decision["work_id"]:
            state.add_dependency_source(decision["work_id"], key)
            attention.enqueue_issue_comment(state, decision, "upstream")
    return filed


# -- external dependency resolution ---------------------------------------


def _external_keys(state):
    """Dependency source keys no local work item owns."""
    owned = {
        attention_key
        for (attention_key,) in (
            (_canonical(row["source_key"]),)
            for row in state.db.execute(
                "SELECT source_key FROM work WHERE source_key IS NOT NULL"
            )
        )
    }
    wanted = {}
    for work in state.list_work():
        if work["phase"] == "completed":
            continue
        for key in work["payload"].get("depends_on_sources", []):
            key = _canonical(key)
            if key not in owned:
                wanted.setdefault(key, []).append(work["id"])
    return wanted


def _canonical(value):
    from .dependencies import canonical_existing

    return canonical_existing(value)


def closing_reference(view):
    for item in view.get("closedByPullRequestsReferences") or []:
        url = item.get("url") or ""
        match = re.search(r"github\.com/([^/]+/[^/]+)/pull/(\d+)", url)
        if match:
            return f"PR {match[1]}#{match[2]}"
        if item.get("number"):
            return f"PR #{item['number']}"
    return "closing PR unknown"


def poll_external(state, adapter, config, resume, *, force=False):
    """Check external dependency issues; returns keys that changed state."""
    row = state.db.execute(
        "SELECT value FROM settings WHERE key='external_poll_at'"
    ).fetchone()
    if not force and row and float(row["value"]) > time.time():
        return []
    with state.db:
        state.db.execute(
            "INSERT OR REPLACE INTO settings VALUES ('external_poll_at',?)",
            (str(time.time() + config.github_poll_seconds),),
        )
    changed = []
    for key, work_ids in _external_keys(state).items():
        match = re.fullmatch(r"github:([^:]+):(\d+)", key or "")
        if not match:
            continue
        slug, number = match[1], int(match[2])
        known = state.db.execute(
            "SELECT state FROM external_issues WHERE key=?", (key,)
        ).fetchone()
        if known and known["state"] != "open":
            continue
        try:
            view = adapter._json(
                [
                    "gh",
                    "issue",
                    "view",
                    str(number),
                    "--repo",
                    slug,
                    "--json",
                    "state,stateReason,closedByPullRequestsReferences",
                ]
            )
        except GitHubFailure as error:
            state.event(
                "external.poll_failed",
                "factory",
                {"issue": key, "code": error.code},
                f"external-poll-failed:{key}:{int(time.time() // 3600)}",
            )
            continue
        if str(view.get("state", "")).upper() != "CLOSED":
            with state.db:
                state.db.execute(
                    "INSERT OR IGNORE INTO external_issues "
                    "(key,slug,number) VALUES (?,?,?)",
                    (key, slug, number),
                )
            continue
        reason = str(view.get("stateReason") or "COMPLETED").upper()
        outcome = "not_planned" if reason == "NOT_PLANNED" else "completed"
        closing = closing_reference(view)
        with state.db:
            state.db.execute(
                "INSERT INTO external_issues (key,slug,number,state,closing,"
                "checked) VALUES (?,?,?,?,?,?) ON CONFLICT(key) DO UPDATE "
                "SET state=excluded.state,closing=excluded.closing,"
                "checked=excluded.checked",
                (key, slug, number, outcome, closing, time.time()),
            )
            state._event(
                "external.closed",
                "factory",
                {"issue": key, "outcome": outcome, "closing": closing},
            )
        changed.append(key)
        for work_id in work_ids:
            if outcome == "completed":
                resume(work_id, slug, number, closing)
            else:
                _ask_not_planned(state, work_id, key, slug, number)
    return changed


def fixed_guidance(slug, number, closing):
    return (
        f"Upstream {slug}#{number} fixed ({closing}); bump the pinned "
        "dependency to include it, then continue."
    )


def _ask_not_planned(state, work_id, key, slug, number):
    attention.add_decision(
        state,
        work_id=work_id,
        kind="free_text",
        question=f"Upstream {slug}#{number} closed as not planned.",
        context=(
            f"Work waits on {key}, which upstream declined. "
            "Choose how to proceed."
        ),
        options=[
            {
                "label": "Rework in the app within Pluto rules",
                "effect": "Resume; implement without the upstream fix.",
                "tradeoff": "May cost more; must not bypass Pluto rules.",
                "recommended": True,
            },
            {
                "label": "Escalate upstream",
                "effect": "Resume after you reopen or re-file upstream.",
                "tradeoff": "Blocked until upstream agrees.",
            },
            {
                "label": "Drop this work",
                "effect": "Resume with guidance to stop.",
                "tradeoff": "Feature not delivered.",
            },
        ],
        proposal={"external": key},
        block_key=f"ext:{key}:{work_id}",
    )
