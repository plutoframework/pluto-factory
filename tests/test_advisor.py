"""Advisor schema, model defaults, framework awareness and issue history."""

import json
import subprocess

import pytest
from pydantic import ValidationError
from test_attention import Gh, build, make_work
from test_dashboard import registered, submit

from pluto_factory import attention, framework_sources
from pluto_factory.advisor import (
    Advice,
    Advisor,
    evidence_packet,
    fallback_advice,
)
from pluto_factory.attention import (
    add_decision,
    answer_decision,
    list_decisions,
)
from pluto_factory.config import (
    ROLE_TEXT,
    advisor_prompt,
    digest,
    resolved_bundle,
)
from pluto_factory.dashboard_data import DashboardData
from pluto_factory.models import FactoryConfig, Repository, Submission
from pluto_factory.state import State


def option(label="Do it", recommended=False, **values):
    return {
        "label": label,
        "effect": "Does the thing.",
        "tradeoff": "Costs time.",
        "recommended": recommended,
        **values,
    }


def advice(**values):
    base = {
        "problem": "Checks fail; flaky db fixture.",
        "details": ["Seen twice."],
        "options": [option("Retry", True), option("Stop")],
        "why": "Cheapest.",
    }
    return {**base, **values}


# --- schema -------------------------------------------------------------


def test_valid_advice_passes():
    parsed = Advice.model_validate(advice())
    assert parsed.options[0].locus == "app"


@pytest.mark.parametrize(
    "bad",
    [
        advice(problem="word " * 41),
        advice(details=["w " * 16]),
        advice(options=[option("Only", True)]),
        advice(options=[option(str(i), i == 0) for i in range(5)]),
        advice(options=[option("A"), option("B")]),  # none recommended
        advice(options=[option("A", True), option("B", True)]),
        advice(options=[option("a b c d e f g h i", True), option("B")]),
        advice(options=[option("A", True, effect="w " * 26), option("B")]),
        advice(why="line one\nline two"),
        advice(options=[option("A", True), option("A")]),
        advice(options=[option("A", True, locus="elsewhere"), option("B")]),
        # framework option needs a one-line fix; app option must not have one
        advice(
            options=[
                option("A", True, locus="framework:pluto-core"),
                option("B"),
            ]
        ),
        advice(options=[option("A", True, fix="x"), option("B")]),
    ],
)
def test_schema_rejects_violations(bad):
    with pytest.raises(ValidationError):
        Advice.model_validate(bad)


def test_framework_locus_option_is_valid_with_fix():
    parsed = Advice.model_validate(
        advice(
            options=[
                option(
                    "Fix pluto-rbac",
                    True,
                    locus="framework:pluto-rbac",
                    fix="Allow scoped wildcard grants in rbac.Check.",
                ),
                option("Work around in app"),
            ]
        )
    )
    assert parsed.options[0].library == "pluto-rbac"


def test_advisor_retries_with_the_violation_then_accepts(tmp_path):
    engine, _ = build(tmp_path)
    calls = []

    def runner(prompt, payload):
        calls.append(json.loads(payload))
        return advice(problem="w " * 50) if len(calls) == 1 else advice()

    _result, source = Advisor(engine.home, engine.config, runner).advise(
        {"block_reason": "x"}
    )
    assert source == "model" and len(calls) == 2
    assert "40" in calls[1]["contract_error"]["problems"]


def test_advisor_falls_back_after_repeated_violations(tmp_path):
    engine, _ = build(tmp_path)
    advisor = Advisor(
        engine.home, engine.config, lambda p, q: advice(options=[])
    )
    result, source = advisor.advise({"block_reason": "disk full on runner"})
    assert source == "fallback"
    assert result.problem.startswith("disk full")
    assert sum(o.recommended for o in result.options) == 1


def test_unknown_framework_library_is_rejected(tmp_path):
    engine, _ = build(tmp_path)
    bad = advice(
        options=[
            option("Fix it", True, locus="framework:nope", fix="Patch nope."),
            option("Stop"),
        ]
    )
    advisor = Advisor(engine.home, engine.config, lambda p, q: bad)
    packet = {
        "block_reason": "x",
        "framework_sources": [{"library": "pluto-core", "version": "v1"}],
    }
    assert advisor.advise(packet)[1] == "fallback"


def test_rules_mode_uses_the_deterministic_fallback(tmp_path):
    engine, _ = build(tmp_path)
    result, source = Advisor(engine.home, engine.config).advise(
        {"block_reason": "a " * 100}
    )
    assert source == "fallback" and len(result.problem.split()) <= 40


# --- model defaults and prompt -------------------------------------------


def config(**values):
    return FactoryConfig(
        name="t",
        repositories=[Repository(name="core", path="/tmp")],
        **values,
    )


def test_advisor_model_defaults_to_the_strongest_tier():
    assert config(harness="claude").advisor_model_name() == "claude-opus-5-5"
    assert (
        config(coordinator_runtime="claude_agent_sdk").advisor_model_name()
        == "claude-opus-5-5"
    )
    assert config().advisor_model_name().endswith("gpt-6.1-sol")
    assert config().advisor.reasoning == "high"


def test_advisor_model_override_and_validation():
    fable = config(harness="claude", advisor_model="claude-fable-5-2")
    assert fable.advisor_model_name() == "claude-fable-5-2"
    with pytest.raises(ValueError):
        config(harness="claude", advisor_model="gpt-6.1-sol")
    with pytest.raises(ValueError):
        config(advisor_model="bad model!")
    with pytest.raises(ValueError):
        config(advisor={"reference_paths": ["relative/path"]})


def test_advisor_claude_options_are_read_only_and_high_effort(tmp_path):
    engine, _ = build(tmp_path)
    config = engine.config.model_copy(update={"harness": "claude"})
    advisor = Advisor(engine.home, config)
    sources = [
        {"library": "pluto-core", "path": str(tmp_path), "version": "v1"}
    ]
    options = advisor.claude_options("prompt", sources)
    assert options.model == "claude-opus-5-5"
    assert options.effort == "high"
    assert set(options.tools) == {"Read", "Grep", "Glob"}
    assert {"Bash", "Write", "Edit"} <= set(options.disallowed_tools)
    assert options.add_dirs == [str(tmp_path)]
    assert options.permission_mode == "dontAsk"


def test_advisor_prompt_carries_the_framework_clause(tmp_path):
    engine, _ = build(tmp_path)
    text = advisor_prompt(engine.home)
    for needle in (
        "pluto-core",
        "pluto-rbac",
        "pluto-auth",
        "pluto-jobs",
        "TypeScript SDK",
        "deficiency in a Pluto library",
        "name the library",
        "recommend the framework fix",
        "Never recommend app-side workarounds",
        "framework:<library>",
        "hard cap 40 words",
    ):
        assert needle in text
    assert text.strip() == ROLE_TEXT["advisor"].strip()


def test_advisor_settings_never_change_the_validation_digest(tmp_path):
    engine, _ = build(tmp_path)
    before = digest(resolved_bundle(engine.home, engine.config))
    changed = engine.config.model_copy(
        update={"advisor_model": "claude-fable-5-2"}
    )
    assert digest(resolved_bundle(engine.home, changed)) == before


def test_orchestrator_prompt_demands_terse_messages():
    text = ROLE_TEXT["orchestrator"].lower()
    assert "telegraphic" in text and "bullets over prose" in text


# --- framework sources ---------------------------------------------------


def test_sources_resolve_pinned_modules_node_modules_and_reference_paths(
    tmp_path, monkeypatch
):
    cache = tmp_path / "modcache"
    core = cache / "github.com/plutoframework/pluto-core@v1.2.3"
    core.mkdir(parents=True)
    (core / "README.md").write_text("core docs")
    (core / "rbac.go").write_text("package core\n// Check grants\n")
    (cache / "github.com/plutoframework/pluto-factory@v0.1.0").mkdir(
        parents=True
    )
    monkeypatch.setenv("GOMODCACHE", str(cache))
    app = tmp_path / "app"
    sdk = app / "node_modules/@plutoframework/sdk"
    sdk.mkdir(parents=True)
    (sdk / "package.json").write_text('{"version": "4.5.6"}')
    (app / "go.mod").write_text(
        "module example.com/app\n\nrequire (\n"
        "\tgithub.com/plutoframework/pluto-core v1.2.3\n"
        "\tgithub.com/plutoframework/pluto-factory v0.1.0\n)\n"
    )
    local = tmp_path / "pluto-jobs"
    local.mkdir()
    cfg = FactoryConfig(
        name="t",
        repositories=[Repository(name="app", path=str(app))],
        advisor={"reference_paths": [str(local)]},
    )
    found = {s["library"]: s for s in framework_sources.resolve(cfg)}
    assert found["pluto-core"]["version"] == "v1.2.3"
    assert found["@plutoframework/sdk"]["version"] == "4.5.6"
    assert found["pluto-jobs"]["origin"] == "reference_paths"
    assert "pluto-factory" not in found
    reader = framework_sources.ReferenceReader(list(found.values()))
    assert "rbac.go" in reader.list_dir("pluto-core")
    assert "Check grants" in reader.read_file("pluto-core", "rbac.go")
    assert reader.search("pluto-core", "Check")
    with pytest.raises(ValueError):
        reader.read_file("pluto-core", "../../../../etc/passwd")
    with pytest.raises(ValueError):
        reader.read_file("unknown", "x")


# --- advised decisions, framework gaps ------------------------------------


def blocked(engine, advisor_reply=None, reason="Auth tests fail: RBAC denies"):
    work = make_work(engine)
    engine._defer(engine.state.get_work(work["id"]), reason)
    if advisor_reply is not None:
        engine.advisor.runner = lambda prompt, payload: advisor_reply
    return work


def test_deferral_gets_a_terse_decision_then_advice_rewrites_it(tmp_path):
    engine, _ = build(tmp_path)
    reason = "Long raw worker paragraph. " * 30
    work = blocked(engine, advice(), reason)
    (pending,) = list_decisions(engine.state, {"open"})
    assert pending["advice"] == "pending"
    assert len(pending["question"].split()) <= 40 + 10
    assert reason.strip()[:40] in pending["context"]
    engine.advise_pending()
    (done,) = list_decisions(engine.state, {"open"})
    assert done["advice"] == "model"
    assert done["question"] == "Checks fail; flaky db fixture."
    assert [o["recommended"] for o in done["options"]] == [True, False]
    assert done["why"] == "Cheapest."
    assert reason.strip()[:40] in done["context"]  # raw kept as evidence
    # A repeat of the same block is still one decision after the rewrite.
    engine._defer(engine.state.get_work(work["id"]), reason)
    assert len(list_decisions(engine.state, {"open"})) == 1


def test_packet_lists_framework_sources(tmp_path):
    engine, _ = build(tmp_path)
    work = blocked(engine)
    (decision,) = list_decisions(engine.state, {"open"})
    packet = evidence_packet(
        engine.state,
        decision,
        "reason",
        sources=[
            {"library": "pluto-core", "path": str(tmp_path), "version": "v1"}
        ],
    )
    assert packet["framework_sources"][0]["library"] == "pluto-core"
    assert packet["work"]["title"] == "Build it"
    del work


FRAMEWORK = advice(
    problem="pluto-rbac denies scoped job grants; library gap.",
    options=[
        option(
            "Fix pluto-rbac",
            True,
            locus="framework:pluto-rbac",
            fix="Support scoped job grants in rbac.Check.",
        ),
        option("Loosen the app role"),
    ],
)


def test_framework_option_records_a_gap_and_keeps_work_parked(tmp_path):
    engine, _gh = build(tmp_path)
    work = make_work_with_issue(engine)
    engine._defer(engine.state.get_work(work["id"]), "rbac denies")
    engine.advisor.runner = lambda p, q: FRAMEWORK
    engine.advise_pending()
    (decision,) = list_decisions(engine.state, {"open"})
    assert decision["options"][0]["locus"] == "framework:pluto-rbac"
    answer_decision(engine.state, decision["id"], "Fix pluto-rbac")
    engine.process_decisions()
    gaps = [
        d for d in list_decisions(engine.state) if d["kind"] == "framework_gap"
    ]
    assert len(gaps) == 1 and gaps[0]["state"] == "open"
    assert "pluto-rbac" in gaps[0]["question"]
    assert engine.state.get_work(work["id"])["control"] == "deferred"
    assert not engine.state.guidance(work["id"])
    kinds = [e["kind"] for e in engine.state.inspect()["events"]]
    assert "framework_gap.recorded" in kinds
    # Comments: created+answered for the original, created for the gap.
    kinds = {
        (r["decision_id"], r["kind"])
        for r in engine.state.db.execute("SELECT * FROM issue_comments")
    }
    assert (gaps[0]["id"], "created") in kinds
    assert (decision["id"], "answered") in kinds
    # A guidance answer to the gap resumes the work.
    answer_decision(engine.state, gaps[0]["id"], attention.GAP_RESUME)
    engine.process_decisions()
    assert engine.state.get_work(work["id"])["control"] == "ready"
    assert engine.state.guidance(work["id"])


def test_app_option_resumes_with_guidance_not_a_gap(tmp_path):
    engine, _ = build(tmp_path)
    work = blocked(engine, FRAMEWORK)
    engine.advise_pending()
    (decision,) = list_decisions(engine.state, {"open"})
    answer_decision(engine.state, decision["id"], "Loosen the app role")
    engine.process_decisions()
    assert engine.state.get_work(work["id"])["control"] == "ready"
    assert not [
        d for d in list_decisions(engine.state) if d["kind"] == "framework_gap"
    ]


def test_pin_bump_resumes_the_parked_work(tmp_path, monkeypatch):
    engine, _ = build(tmp_path)
    work = blocked(engine, FRAMEWORK)
    pins = {"pluto-rbac": "v1.0.0"}
    monkeypatch.setattr("pluto_factory.engine.pins", lambda sources: dict(pins))
    monkeypatch.setattr(engine.advisor, "sources", list)
    engine.advise_pending()
    (decision,) = list_decisions(engine.state, {"open"})
    answer_decision(engine.state, decision["id"], "Fix pluto-rbac")
    engine.process_decisions()
    engine.check_framework_gaps()
    assert engine.state.get_work(work["id"])["control"] == "deferred"
    pins["pluto-rbac"] = "v1.1.0"
    engine.attention()
    engine.attention()
    resumed = engine.state.get_work(work["id"])
    assert resumed["control"] == "ready"
    assert "pin bumped" in engine.state.guidance(work["id"])[0]["text"]


# --- GitHub issue history -------------------------------------------------


class IssueGh(Gh):
    def __init__(self):
        super().__init__()
        self.comments = []
        self.fail_next = 0

    def __call__(self, argv, **kwargs):
        if argv[:3] == ["gh", "issue", "view"]:
            self.calls.append(list(argv))
            body = json.dumps(
                {"comments": [{"body": b} for b in self.comments]}
            )
            return subprocess.CompletedProcess(argv, 0, body, "")
        if argv[:3] == ["gh", "issue", "comment"]:
            self.calls.append(list(argv))
            if self.fail_next:
                self.fail_next -= 1
                return subprocess.CompletedProcess(argv, 1, "", "HTTP 502")
            self.comments.append(argv[argv.index("--body") + 1])
            return subprocess.CompletedProcess(argv, 0, "", "")
        return super().__call__(argv, **kwargs)


def make_work_with_issue(engine, number="12"):
    return engine.state.submit(
        Submission(
            title="Issue work",
            instruction="i",
            acceptance=["a"],
            repositories=["core"],
            source="github:owner/core",
            delivery_key=number,
        )
    )


def issue_engine(tmp_path):
    engine, _ = build(tmp_path)
    gh = IssueGh()
    engine.tracker.adapter.runner = gh
    return engine, gh


def test_created_comment_posted_once_and_idempotent(tmp_path):
    engine, gh = issue_engine(tmp_path)
    work = make_work_with_issue(engine)
    engine.advisor.runner = lambda p, q: advice()
    engine._defer(engine.state.get_work(work["id"]), "needs a call")
    # Not posted before the advisor has produced the headline and options.
    assert engine.flush_issue_comments() == 0
    engine.advise_pending()
    assert engine.flush_issue_comments() == 1
    (body,) = gh.comments
    (decision,) = list_decisions(engine.state, {"open"})
    assert attention.marker(decision["id"], "created") in body
    assert "Checks fail; flaky db fixture." in body
    assert "Retry** (recommended)" in body and "Stop" in body
    assert "Recommended because: Cheapest." in body
    # Re-flushing, or a lost outbox row, never double-posts.
    assert engine.flush_issue_comments() == 0
    engine.state.db.execute("UPDATE issue_comments SET state='pending'")
    engine.state.db.commit()
    assert engine.flush_issue_comments() == 0
    assert len(gh.comments) == 1


def test_comment_post_retries_after_failure(tmp_path):
    engine, gh = issue_engine(tmp_path)
    work = make_work_with_issue(engine)
    engine.advisor.runner = lambda p, q: advice()
    engine._defer(engine.state.get_work(work["id"]), "needs a call")
    engine.advise_pending()
    gh.fail_next = 1
    assert engine.flush_issue_comments() == 0
    row = engine.state.db.execute("SELECT * FROM issue_comments").fetchone()
    assert row["state"] == "pending" and row["attempts"] == 1
    assert row["wake_at"] > row["created"]
    engine.state.db.execute("UPDATE issue_comments SET wake_at=0")
    engine.state.db.commit()
    assert engine.flush_issue_comments() == 1 and len(gh.comments) == 1


def test_answered_comment_names_answer_who_and_when(tmp_path):
    engine, gh = issue_engine(tmp_path)
    work = make_work_with_issue(engine)
    engine.advisor.runner = lambda p, q: advice()
    engine._defer(engine.state.get_work(work["id"]), "needs a call")
    engine.advise_pending()
    (decision,) = list_decisions(engine.state, {"open"})
    answer_decision(
        engine.state,
        decision["id"],
        "Retry",
        "but use sqlite",
        via="dashboard",
        by="david@example.com",
    )
    engine.attention()
    engine.attention()
    _created, answered = gh.comments
    assert attention.marker(decision["id"], "answered") in answered
    assert "Answer: Retry" in answered
    assert "Custom feedback: but use sqlite" in answered
    assert "By: david@example.com" in answered
    assert "At: 20" in answered and "Z" in answered
    engine.attention()
    assert len(gh.comments) == 2


def test_answered_by_defaults_to_owner_and_no_issue_means_no_comment(tmp_path):
    engine, gh = issue_engine(tmp_path)
    work = make_work(engine)  # no source issue
    engine._defer(engine.state.get_work(work["id"]), "needs a call")
    (decision,) = list_decisions(engine.state, {"open"})
    answer_decision(engine.state, decision["id"], "Retry as planned")
    stored = attention.get_decision(engine.state, decision["id"])
    assert stored["answered_by"] == "owner"
    engine.attention()
    assert not gh.comments
    assert (
        engine.state.db.execute("SELECT * FROM issue_comments").fetchall() == []
    )


def test_unregistered_issue_repository_is_never_commented(tmp_path):
    engine, gh = issue_engine(tmp_path)
    decision = add_decision(
        engine.state,
        question="q",
        kind="free_text",
        work_id=make_work_with_issue(engine)["id"],
    )
    engine.state.db.execute("UPDATE issue_comments SET slug='evil/repo'")
    engine.state.db.commit()
    assert engine.flush_issue_comments() == 0
    assert not gh.comments
    del decision


# --- dashboard projection ---------------------------------------------------


def test_card_shows_terse_problem_and_detail_carries_the_decision(tmp_path):
    home = registered(tmp_path)
    state = State(home)
    work = submit(state)
    attention.add_decision(
        state,
        question="Checks fail; flaky db.",
        context="RAW " * 200,
        kind="free_text",
        work_id=work["id"],
        details=["Seen twice."],
        why="Cheapest.",
        options=[
            {
                "label": "Fix pluto-rbac",
                "effect": "Patch it.",
                "tradeoff": "Slow.",
                "locus": "framework:pluto-rbac",
                "fix": "Support scoped grants.",
                "recommended": True,
            },
            {"label": "Stop"},
        ],
    )
    state.update_work(work["id"], control="deferred")
    data = DashboardData(tmp_path)
    factory = data.state()["factories"][0]
    card = factory["work"][0]
    assert card["latest_failure"] == "Checks fail; flaky db."
    assert "RAW" not in json.dumps(card["decision"]["question"])
    detail = data.detail(factory["id"], work["id"])["work"]
    decision = detail["decision"]
    assert decision["details"] == ["Seen twice."]
    assert decision["evidence"].startswith("RAW")
    assert decision["options"][0]["locus"] == "framework:pluto-rbac"
    assert decision["options"][0]["fix"] == "Support scoped grants."
    assert decision["options"][1]["locus"] == "app"
    needs = data.state()["needs_you"]["decisions"][0]
    assert needs["question"] == "Checks fail; flaky db."
    state.close()


def test_dashboard_assets_render_framework_tag_and_both_views():
    from importlib.resources import files

    root = files("pluto_factory").joinpath("dashboard")
    js = root.joinpath("app.js").read_text()
    css = root.joinpath("styles.css").read_text()
    assert 'startsWith("framework:")' in js and "Framework: " in js
    assert "framework-tag" in js and ".framework-tag" in css
    assert "Raw block text" in js and "Custom feedback" in js
    # One card renderer serves the Needs-you list and the detail panel.
    assert "map(decisionCard)" in js and "decisionCard(work.decision" in js
    assert 'section("Needs you")' in js
    assert "Recommended" in js


def test_fallback_advice_is_schema_valid():
    for pr in (False, True):
        Advice.model_validate(
            fallback_advice("x " * 90, pull_request=pr).model_dump()
        )


def test_stale_coordinator_prompts_still_get_the_terse_rule():
    from pluto_factory.config import TERSE_STYLE
    from pluto_factory.coordinator import _styled

    assert TERSE_STYLE in _styled("old prompt")
    assert _styled(ROLE_TEXT["orchestrator"]) == ROLE_TEXT["orchestrator"]
