"""Upstream framework issues over GitHub, and external dependency polling."""

import json
import subprocess

from test_advisor import FRAMEWORK, IssueGh, advice, make_work_with_issue
from test_attention import build

from pluto_factory import attention
from pluto_factory.attention import answer_decision, list_decisions
from pluto_factory.models import FrameworkPolicy, Submission

UPSTREAM = "plutoframework/pluto-core"
KEY = "github:plutoframework/pluto-core:7"


class UpstreamGh(IssueGh):
    def __init__(self):
        super().__init__()
        self.created = []
        self.remote = {}  # (slug, number) -> issue view JSON
        self.fail_create = 0

    def __call__(self, argv, **kwargs):
        if argv[:3] == ["gh", "issue", "create"]:
            self.calls.append(list(argv))
            if self.fail_create:
                self.fail_create -= 1
                return subprocess.CompletedProcess(argv, 1, "", "HTTP 502")
            slug = argv[argv.index("--repo") + 1]
            number = 7 + len(self.created)
            self.created.append(
                {
                    "slug": slug,
                    "number": number,
                    "title": argv[argv.index("--title") + 1],
                    "body": argv[argv.index("--body") + 1],
                    "label": argv[argv.index("--label") + 1],
                }
            )
            return subprocess.CompletedProcess(
                argv, 0, f"https://github.com/{slug}/issues/{number}\n", ""
            )
        if argv[:3] == ["gh", "issue", "list"]:
            self.calls.append(list(argv))
            slug = argv[argv.index("--repo") + 1]
            rows = [
                {
                    "number": i["number"],
                    "url": f"https://github.com/{slug}/issues/{i['number']}",
                    "body": i["body"],
                }
                for i in self.created
                if i["slug"] == slug
            ]
            return subprocess.CompletedProcess(argv, 0, json.dumps(rows), "")
        if (
            argv[:3] == ["gh", "issue", "view"]
            and "state,stateReason,closedByPullRequestsReferences" in argv
        ):
            self.calls.append(list(argv))
            slug = argv[argv.index("--repo") + 1]
            view = self.remote.get((slug, int(argv[3])), {"state": "OPEN"})
            return subprocess.CompletedProcess(argv, 0, json.dumps(view), "")
        return super().__call__(argv, **kwargs)


def setup(tmp_path, mode="on_decision", mapped=True):
    framework = FrameworkPolicy(
        upstream={"pluto-rbac": UPSTREAM} if mapped else {},
        file_upstream=mode,
    )
    engine, _ = build(tmp_path, framework=framework)
    gh = UpstreamGh()
    engine.tracker.adapter.runner = gh
    work = make_work_with_issue(engine)
    engine.advisor.runner = lambda p, q: FRAMEWORK
    engine._defer(engine.state.get_work(work["id"]), "RBAC denies job grant")
    engine.advise_pending()
    return engine, gh, work


def choose_framework(engine):
    (decision,) = list_decisions(engine.state, {"open"})
    answer_decision(engine.state, decision["id"], "Fix pluto-rbac")
    engine.process_decisions()
    return decision


def test_owner_choice_files_one_upstream_issue_with_links(tmp_path):
    engine, gh, work = setup(tmp_path)
    assert not gh.created  # nothing filed until the owner picks the option
    decision = choose_framework(engine)
    assert engine.flush_upstream() == 1
    (issue,) = gh.created
    assert issue["slug"] == UPSTREAM and issue["label"] == "factory:proposed"
    # A foreign factory never marks work ready.
    assert "factory:ready" not in json.dumps(gh.calls)
    assert engine.config.github_issue_label not in issue["label"]
    body = issue["body"]
    assert attention.get_decision(engine.state, decision["id"])
    assert issue["title"] == "RBAC check cannot scope job grants"
    assert "pluto-factory:upstream:test" not in body
    assert decision["id"] not in body and "Evidence" not in body
    assert "owner/core" not in body and work["id"] not in body
    # Private back-link lives only on the private side.
    assert "Origin" not in body and "Factory" not in body
    assert "## Acceptance criteria" in body
    # Raw evidence is never copied.
    assert len(body) < 4000
    # Source issue comment + dependency on the upstream issue.
    engine.flush_issue_comments()
    assert any(
        f"https://github.com/{UPSTREAM}/issues/7" in c for c in gh.comments
    )
    stored = engine.state.get_work(work["id"])
    assert KEY in stored["payload"]["depends_on_sources"]
    assert not engine.state.dependency_status(work["id"])["ready"]


def test_filing_is_idempotent_and_adopts_after_a_crash(tmp_path):
    engine, gh, _work = setup(tmp_path)
    choose_framework(engine)
    engine.flush_upstream()
    engine.flush_upstream()
    assert len(gh.created) == 1
    # Crash after creating, before recording the number: the row is pending.
    engine.state.db.execute(
        "UPDATE upstream_issues SET state='pending',number=NULL,url=''"
    )
    engine.state.db.commit()
    assert engine.flush_upstream() == 0
    assert len(gh.created) == 1
    row = engine.state.db.execute("SELECT * FROM upstream_issues").fetchone()
    assert row["state"] == "filed" and row["number"] == 7
    # Answering again or re-requesting never duplicates the request.
    (decision,) = [
        d for d in list_decisions(engine.state) if d["state"] == "applied"
    ]
    option = next(o for o in decision["options"] if o["locus"] != "app")
    from pluto_factory.upstream import request_upstream

    assert not request_upstream(engine.state, engine.config, decision, option)


def test_failed_filing_retries_with_backoff(tmp_path):
    engine, gh, _ = setup(tmp_path)
    choose_framework(engine)
    gh.fail_create = 1
    assert engine.flush_upstream() == 0
    row = engine.state.db.execute("SELECT * FROM upstream_issues").fetchone()
    assert row["state"] == "pending" and row["attempts"] == 1
    engine.state.db.execute("UPDATE upstream_issues SET wake_at=0")
    engine.state.db.commit()
    assert engine.flush_upstream() == 1


def test_auto_mode_files_when_the_recommended_option_is_framework(tmp_path):
    engine, gh, work = setup(tmp_path, mode="auto")
    assert engine.flush_upstream() == 1  # before any owner answer
    assert len(gh.created) == 1
    stored = engine.state.get_work(work["id"])
    assert KEY in stored["payload"]["depends_on_sources"]


def test_auto_mode_ignores_app_locus_recommendations(tmp_path):
    engine, _ = build(
        tmp_path,
        framework=FrameworkPolicy(
            upstream={"pluto-rbac": UPSTREAM}, file_upstream="auto"
        ),
    )
    gh = UpstreamGh()
    engine.tracker.adapter.runner = gh
    work = make_work_with_issue(engine)
    engine.advisor.runner = lambda p, q: advice()
    engine._defer(engine.state.get_work(work["id"]), "x")
    engine.advise_pending()
    assert engine.flush_upstream() == 0 and not gh.created


def test_unmapped_library_files_nothing_but_asks_the_owner(tmp_path):
    engine, gh, work = setup(tmp_path, mapped=False)
    choose_framework(engine)
    assert engine.flush_upstream() == 0 and not gh.created
    kinds = [e["kind"] for e in engine.state.inspect()["events"]]
    assert "upstream.unmapped" in kinds
    (ask,) = [
        d
        for d in list_decisions(engine.state, {"open"})
        if (d["proposal"] or {}).get("upstream_repo_for")
    ]
    assert ask["question"] == "Upstream repo unknown for pluto-rbac."
    assert ask["options"][-1]["label"] == "Withdraw; handle in the app"
    # Withdrawing drops the gap and resumes the work with guidance.
    answer_decision(engine.state, ask["id"], "Withdraw; handle in the app")
    engine.process_decisions()
    assert engine.state.get_work(work["id"])["control"] == "ready"
    assert not [
        d
        for d in list_decisions(engine.state, {"open"})
        if d["kind"] == "framework_gap"
    ]


def test_owner_can_pick_a_mapped_repo_for_an_unmapped_library(tmp_path):
    engine, gh, work = setup(tmp_path, mapped=False)
    engine.config.framework.upstream = {"pluto-core": UPSTREAM}
    choose_framework(engine)
    (ask,) = [
        d
        for d in list_decisions(engine.state, {"open"})
        if (d["proposal"] or {}).get("upstream_repo_for")
    ]
    assert f"File in {UPSTREAM}" in [o["label"] for o in ask["options"]]
    answer_decision(engine.state, ask["id"], f"File in {UPSTREAM}")
    engine.process_decisions()
    assert engine.flush_upstream() == 1
    assert gh.created[0]["slug"] == UPSTREAM
    assert engine.state.get_work(work["id"])["control"] == "deferred"


def test_library_names_normalize_the_pluto_prefix():
    policy = FrameworkPolicy(upstream={"pluto-cli": "plutoframework/cli"})
    for name in ("cli", "pluto-cli", "CLI", "@plutoframework/cli"):
        assert policy.repository(name) == "plutoframework/cli"
    assert policy.repository("auth") == ""


def test_framework_config_validates_repositories():
    import pytest

    with pytest.raises(ValueError):
        FrameworkPolicy(upstream={"pluto-core": "not-a-repo"})
    policy = FrameworkPolicy()
    assert policy.proposed_label == "factory:proposed"
    assert policy.file_upstream == "on_decision"
    assert (
        FrameworkPolicy(upstream={"@plutoframework/sdk": "o/sdk"}).repository(
            "sdk"
        )
        == "o/sdk"
    )


# --- external dependencies ----------------------------------------------


def test_closed_completed_upstream_resumes_with_guidance(tmp_path):
    engine, gh, work = setup(tmp_path)
    choose_framework(engine)
    engine.flush_upstream()
    engine.attention(force=True)
    assert engine.state.get_work(work["id"])["control"] == "deferred"
    gh.remote[(UPSTREAM, 7)] = {
        "state": "CLOSED",
        "stateReason": "COMPLETED",
        "closedByPullRequestsReferences": [
            {"number": 9, "url": f"https://github.com/{UPSTREAM}/pull/9"}
        ],
    }
    engine.attention(force=True)
    resumed = engine.state.get_work(work["id"])
    assert resumed["control"] == "ready"
    assert engine.state.dependency_status(work["id"])["ready"]
    (guidance,) = engine.state.guidance(work["id"])
    assert guidance["text"] == (
        f"Upstream {UPSTREAM}#7 fixed (PR {UPSTREAM}#9); bump the pinned "
        "dependency to include it, then continue."
    )
    # The parked framework-gap decision is withdrawn; nothing repeats.
    assert not [
        d
        for d in list_decisions(engine.state, {"open"})
        if d["kind"] == "framework_gap"
    ]
    engine.attention(force=True)
    assert len(engine.state.guidance(work["id"])) == 1


def test_open_upstream_keeps_work_parked(tmp_path):
    engine, _gh, work = setup(tmp_path)
    choose_framework(engine)
    engine.flush_upstream()
    for _ in range(2):
        engine.attention(force=True)
    assert engine.state.get_work(work["id"])["control"] == "deferred"
    assert not engine.state.guidance(work["id"])


def test_plain_external_dependency_resolves_without_filing(tmp_path):
    engine, _ = build(tmp_path)
    gh = UpstreamGh()
    engine.tracker.adapter.runner = gh
    work = engine.state.submit(
        Submission(
            title="w", instruction="i", acceptance=["a"], repositories=["core"]
        )
    )
    engine.state.add_dependency_source(work["id"], KEY)
    assert not engine.state.dependency_status(work["id"])["ready"]
    gh.remote[(UPSTREAM, 7)] = {"state": "CLOSED", "stateReason": None}
    engine.attention(force=True)
    assert engine.state.dependency_status(work["id"])["ready"]
    (guidance,) = engine.state.guidance(work["id"])
    assert "closing PR unknown" in guidance["text"]


def test_not_planned_upstream_creates_a_decision(tmp_path):
    engine, gh, work = setup(tmp_path)
    choose_framework(engine)
    engine.flush_upstream()
    gh.remote[(UPSTREAM, 7)] = {"state": "CLOSED", "stateReason": "NOT_PLANNED"}
    engine.attention(force=True)
    (decision,) = [
        d
        for d in list_decisions(engine.state, {"open"})
        if (d["proposal"] or {}).get("external")
    ]
    assert "closed as not planned" in decision["question"]
    assert any(o["recommended"] for o in decision["options"])
    assert not engine.state.guidance(work["id"])  # not resumed on its own
    assert not engine.state.dependency_status(work["id"])["ready"]
    # Repeated polls do not duplicate the decision.
    engine.attention(force=True)
    assert (
        len(
            [
                d
                for d in list_decisions(engine.state, {"open"})
                if (d["proposal"] or {}).get("external")
            ]
        )
        == 1
    )
    answer_decision(
        engine.state, decision["id"], "Rework in the app within Pluto rules"
    )
    engine.process_decisions()
    assert engine.state.dependency_status(work["id"])["ready"]
    assert engine.state.get_work(work["id"])["control"] == "ready"


def test_upstream_dependency_does_not_break_issue_redelivery(tmp_path):
    engine, _gh, work = setup(tmp_path)
    choose_framework(engine)
    engine.flush_upstream()
    again = engine.state.submit(
        Submission(
            title="Issue work",
            instruction="i",
            acceptance=["a"],
            repositories=["core"],
            source="github:owner/core",
            delivery_key="12",
        )
    )
    assert again["id"] == work["id"]


def test_proposed_label_can_never_equal_the_ready_label():
    import pytest

    from pluto_factory.models import FactoryConfig, Repository

    with pytest.raises(ValueError):
        FactoryConfig(
            name="t",
            repositories=[Repository(name="a", path="/tmp")],
            framework={"proposed_label": "factory:ready"},
        )


# --- redaction ------------------------------------------------------------


def _filed(engine, gh):
    choose_framework(engine)
    engine.flush_upstream()
    return gh.created


def test_filed_issue_contains_no_private_tokens(tmp_path):
    engine, gh, work = setup(tmp_path)
    (issue,) = _filed(engine, gh)
    text = issue["title"] + "\n" + issue["body"]
    config = engine.config
    for token in (
        config.name,
        "owner/core",
        work["id"],
        "Issue work",
        "#12",
        str(tmp_path),
        "checkout",
    ):
        assert token.lower() not in text.lower(), token


def test_marker_is_opaque_and_stable(tmp_path):
    from pluto_factory.upstream import upstream_marker

    engine, gh, _ = setup(tmp_path)
    (issue,) = _filed(engine, gh)
    marker = upstream_marker(
        engine.state, attention.list_decisions(engine.state)[0]["id"]
    )
    assert marker in issue["body"]
    assert upstream_marker(engine.state, "decision-x") == upstream_marker(
        engine.state, "decision-x"
    )
    assert upstream_marker(engine.state, "decision-x") != upstream_marker(
        engine.state, "decision-y"
    )
    import re

    assert re.fullmatch(r"<!-- pluto-factory:upstream:[0-9a-f]{16} -->", marker)


def _leak(engine, **fields):
    """Rewrite the stored framework option's upstream payload."""
    import json as j

    (decision,) = list_decisions(engine.state, {"open"})
    options = decision["options"]
    options[0]["upstream"] = {**options[0]["upstream"], **fields}
    engine.state.db.execute(
        "UPDATE decisions SET options=? WHERE id=?",
        (j.dumps(options), decision["id"]),
    )
    engine.state.db.commit()


def test_scrubber_blocks_filing_and_creates_a_hold_decision(tmp_path):
    engine, gh, work = setup(tmp_path)
    _leak(
        engine,
        problem=f"Okta in test breaks, see owner/core#12 and #12, "
        f"{work['id']}, commit abcdef1 at {tmp_path}/checkout/x.",
    )
    assert not _filed(engine, gh)
    row = engine.state.db.execute("SELECT * FROM upstream_issues").fetchone()
    assert row["state"] == "held"
    (hold,) = [
        d
        for d in list_decisions(engine.state, {"open"})
        if (d["proposal"] or {}).get("upstream_hold")
    ]
    for hit in ("private term: test", "owner/core#12", "#12", "abcdef1"):
        assert hit in hold["context"]
    assert [o["label"] for o in hold["options"]] == [
        "Edit and file",
        "Skip upstream filing",
    ]
    # Edit with a clean replacement: filed.
    answer_decision(
        engine.state,
        hold["id"],
        "Edit and file",
        "Per-tenant providers cannot be scoped by the grant check.",
    )
    engine.process_decisions()
    engine.flush_upstream()
    (issue,) = gh.created
    assert "Per-tenant providers" in issue["body"]


def test_hold_skip_never_files(tmp_path):
    engine, gh, _ = setup(tmp_path)
    _leak(engine, repro="see #99")
    assert not _filed(engine, gh)
    (hold,) = [
        d
        for d in list_decisions(engine.state, {"open"})
        if (d["proposal"] or {}).get("upstream_hold")
    ]
    answer_decision(engine.state, hold["id"], "Skip upstream filing")
    engine.process_decisions()
    engine.flush_upstream()
    assert not gh.created
    row = engine.state.db.execute("SELECT * FROM upstream_issues").fetchone()
    assert row["state"] == "skipped"


def test_redact_terms_are_enforced(tmp_path):
    engine, _ = build(
        tmp_path,
        framework=FrameworkPolicy(
            upstream={"pluto-rbac": UPSTREAM}, redact_terms=["Okta"]
        ),
    )
    gh = UpstreamGh()
    engine.tracker.adapter.runner = gh
    work = make_work_with_issue(engine)
    engine.advisor.runner = lambda p, q: FRAMEWORK
    engine._defer(engine.state.get_work(work["id"]), "x")
    engine.advise_pending()
    _leak(engine, problem="Okta needs per-tenant providers.")
    assert not _filed(engine, gh)
