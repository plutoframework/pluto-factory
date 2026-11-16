"""Cross-factory work through GitHub issues only (no factory-to-factory RPC).

A chosen framework option files an issue in the mapped upstream repository
(the label its factory's intake watches), links it on the source issue, and
makes the blocked work wait on it. External dependencies are then polled on
the GitHub cadence: closed-as-completed resumes the work, not-planned asks
the owner.
"""

import hashlib
import hmac
import json
import os
import re
import secrets
import time

from . import attention
from .github import GitHubFailure

SLUG = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
ISSUE_URL = re.compile(r"https://github\.com/([^/]+/[^/]+)/issues/(\d+)")
SECRET_KEY = "upstream_secret"
HOLD_SKIP = "Skip upstream filing"
HOLD_EDIT = "Edit and file"
SLUG_REF = re.compile(r"(?<![\w/.-])([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)#(\d+)")
BARE_REF = re.compile(r"(?<![\w/.#&-])#(\d+)\b")
SHA = re.compile(r"(?<![0-9A-Za-z])[0-9a-fA-F]{7,40}(?![0-9A-Za-z])")


def factory_secret(state):
    """Random per-factory secret, created once and kept in state."""
    with state.db:
        state.db.execute(
            "INSERT OR IGNORE INTO settings VALUES (?,?)",
            (SECRET_KEY, secrets.token_hex(32)),
        )
    return state.db.execute(
        "SELECT value FROM settings WHERE key=?", (SECRET_KEY,)
    ).fetchone()["value"]


def upstream_marker(state, decision_id):
    """Opaque, stable, idempotent: reveals neither factory nor decision."""
    digest = hmac.new(
        factory_secret(state).encode(), decision_id.encode(), hashlib.sha256
    ).hexdigest()[:16]
    return f"<!-- pluto-factory:upstream:{digest} -->"


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


# -- public payload and scrubber --------------------------------------------


def payload_for(state, decision, option):
    """The generic payload, with an owner edit (if any) as the problem."""
    payload = dict(option.get("upstream") or {})
    edit = state.db.execute(
        "SELECT value FROM settings WHERE key=?",
        (f"upstream_edit:{decision['id']}",),
    ).fetchone()
    if edit and payload:
        payload["problem"] = edit["value"]
    return payload


def render_title(payload):
    return " ".join(str(payload.get("title") or "").split())[:80]


def render_body(state, decision, payload):
    lines = [
        upstream_marker(state, decision["id"]),
        "## Problem",
        payload["problem"],
        "",
        "## Minimal repro",
        payload["repro"],
        "",
        "## Proposed fix",
        payload["fix"],
        "",
        "## Acceptance criteria",
        *[f"- [ ] {item}" for item in payload["acceptance"]],
    ]
    return "\n".join(lines)


def _fields(payload):
    return [
        str(payload.get("title") or ""),
        str(payload.get("problem") or ""),
        str(payload.get("repro") or ""),
        str(payload.get("fix") or ""),
        *[str(item) for item in payload.get("acceptance") or []],
    ]


def denylist(state, config):
    """Private terms and paths that must never reach a public issue."""
    terms = {config.name}
    paths = {os.path.expanduser("~"), os.environ.get("HOME", "")}
    for repo in config.repositories:
        terms.update({repo.name, repo.github or ""})
        paths.add(repo.path)
    for tracked in config.tracked_repositories:
        terms.update({tracked.repo})
        paths.add(tracked.local_path)
    terms.update(config.framework.redact_terms)
    for work in state.list_work():
        terms.update({work["id"], work["title"]})
    terms = {t.strip() for t in terms if t and len(t.strip()) >= 2}
    paths = {
        os.path.normpath(p) for p in paths if p and len(os.path.normpath(p)) > 1
    }
    return terms, paths


def _exists(adapter, argv, key, prefix):
    try:
        data = adapter._json(argv)
    except Exception:  # noqa: BLE001 - unverifiable means suspect
        return False
    return str(data.get(key, "")).lower().startswith(prefix.lower())


def scrub(state, adapter, config, upstream_slug, fields):
    """Hits found in any field; empty means clean. Fails closed."""
    terms, paths = denylist(state, config)
    hits = []
    text = "\n".join(fields)
    for term in sorted(terms):
        pattern = rf"(?<![A-Za-z0-9_-]){re.escape(term)}(?![A-Za-z0-9_-])"
        if re.search(pattern, text, re.IGNORECASE):
            hits.append(f"private term: {term}")
    for path in sorted(paths):
        if path in text:
            hits.append(f"private path: {path}")
    for slug, number in SLUG_REF.findall(text):
        if slug.lower() != upstream_slug.lower():
            hits.append(f"issue reference: {slug}#{number}")
    for number in sorted(set(BARE_REF.findall(text)), key=int):
        if not _exists(
            adapter,
            [
                "gh",
                "issue",
                "view",
                number,
                "--repo",
                upstream_slug,
                "--json",
                "number",
            ],
            "number",
            number,
        ):
            hits.append(f"issue reference: #{number}")
    for sha in sorted(set(SHA.findall(text))):
        if not any(c.isdigit() for c in sha):
            continue
        if not _exists(
            adapter,
            ["gh", "api", f"repos/{upstream_slug}/commits/{sha}"],
            "sha",
            sha,
        ):
            hits.append(f"commit SHA: {sha}")
    return list(dict.fromkeys(hits))


def _hold(state, decision, row, hits, payload):
    """Park the filing and ask the owner; never file with hits present."""
    with state.db:
        state.db.execute(
            "UPDATE upstream_issues SET state='held',last_error=? WHERE id=?",
            ("upstream.redaction", row["id"]),
        )
    attention.add_decision(
        state,
        work_id=decision["work_id"],
        kind="choice",
        question=(
            f"Upstream issue for {row['library']} blocked: private details "
            f"found ({len(hits)})."
        )[:500],
        context=(
            "Not filed. Private details found in the public issue text:\n"
            + "\n".join(f"- {hit}" for hit in hits)
            + f"\n\nTitle: {render_title(payload)}"
        ),
        options=[
            {
                "label": HOLD_EDIT,
                "effect": (
                    "Answer with replacement problem text (no private "
                    "details); filing is retried."
                ),
                "recommended": True,
            },
            {
                "label": HOLD_SKIP,
                "effect": "Never file upstream; work stays parked.",
            },
        ],
        proposal={
            "upstream_hold": decision["id"],
            "library": row["library"],
        },
        block_key="upstream-hold:"
        + hashlib.sha256(
            json.dumps([decision["id"], hits, payload], sort_keys=True).encode()
        ).hexdigest()[:20],
        notify=True,
    )
    state.event(
        "upstream.held",
        decision["work_id"] or "factory",
        {"decision": decision["id"], "hits": len(hits)},
    )


def apply_hold_answer(state, decision):
    """Owner answered a redaction hold: skip, or retry with edited text."""
    proposal = decision["proposal"]
    origin, library = proposal["upstream_hold"], proposal["library"]
    if not decision["custom"] and decision["answer"] == HOLD_SKIP:
        new_state = "skipped"
    else:
        edit = (
            decision["answer"]
            if decision["custom"]
            else (decision.get("note") or "")
        ).strip()
        if edit:
            with state.db:
                state.db.execute(
                    "INSERT OR REPLACE INTO settings VALUES (?,?)",
                    (f"upstream_edit:{origin}", edit),
                )
        new_state = "pending"
    with state.db:
        state.db.execute(
            "UPDATE upstream_issues SET state=?,wake_at=0,attempts=0,"
            "last_error=NULL WHERE decision_id=? AND library=? "
            "AND state='held'",
            (new_state, origin, library),
        )


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
            tag = upstream_marker(state, decision["id"])
            payload = payload_for(state, decision, option)
            if not payload:
                hits = ["no upstream payload recorded for this option"]
            else:
                hits = scrub(
                    state,
                    adapter,
                    config,
                    row["slug"],
                    _fields(payload),
                )
            if hits:
                _hold(state, decision, row, hits, payload)
                continue
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
                        render_title(payload),
                        "--body",
                        render_body(state, decision, payload),
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
