"""Advisor autonomy: deterministic gate, grace window, override."""

import time

import pytest
from test_advisor import UPSTREAM_PAYLOAD, advice, make_work_with_issue, option
from test_attention import build

from pluto_factory import attention
from pluto_factory.advisor import Advice
from pluto_factory.attention import (
    answer_decision,
    apply_due_autos,
    auto_verdict,
    list_decisions,
)
from pluto_factory.models import AutonomyPolicy, FrameworkPolicy

UP = "plutoframework/pluto-core"
AUTO = {
    "category": "app_fix",
    "autonomy": "auto",
    "autonomy_reason": "Obvious reversible fix.",
    "confidence": "high",
    "reversible": True,
}


def auto_advice(**values):
    return advice(**{**AUTO, **values})


def run(tmp_path, reply, **settings):
    engine, _ = build(tmp_path, **settings)
    work = make_work_with_issue(engine)
    engine.advisor.runner = lambda p, q: reply
    engine._defer(engine.state.get_work(work["id"]), "flaky fixture")
    engine.advise_pending()
    return engine, work


def states(engine):
    return [d["state"] for d in list_decisions(engine.state)]


def test_obvious_decision_waits_then_applies_as_advisor(tmp_path):
    engine, work = run(
        tmp_path,
        auto_advice(),
        autonomy=AutonomyPolicy(grace_seconds=600),
    )
    (decision,) = list_decisions(engine.state)
    assert decision["state"] == "auto_pending"
    assert decision["auto"]["reason"] == "Obvious reversible fix."
    # Not counted as needing the owner; nothing applies inside the window.
    assert not list_decisions(engine.state, {"open"})
    assert apply_due_autos(engine.state) == []
    assert apply_due_autos(engine.state, time.time() + 601) == [decision["id"]]
    engine.process_decisions()
    done = attention.get_decision(engine.state, decision["id"])
    assert done["state"] == "applied"
    assert done["answered_by"] == "advisor (auto)"
    assert engine.state.get_work(work["id"])["control"] == "ready"
    comments = [
        r["kind"]
        for r in engine.state.db.execute("SELECT kind FROM issue_comments")
    ]
    assert "auto" in comments and "created" not in comments


def test_owner_answer_in_grace_window_wins(tmp_path):
    engine, _ = run(tmp_path, auto_advice())
    (decision,) = list_decisions(engine.state)
    answer_decision(engine.state, decision["id"], "Stop; I will handle it")
    assert apply_due_autos(engine.state, time.time() + 9999) == []
    got = attention.get_decision(engine.state, decision["id"])
    assert (
        got["answered_by"] == "owner" and got["auto"]["status"] == "overridden"
    )


def test_override_after_apply_resumes_with_new_guidance(tmp_path):
    engine, work = run(
        tmp_path, auto_advice(), autonomy=AutonomyPolicy(grace_seconds=0)
    )
    (decision,) = list_decisions(engine.state)
    apply_due_autos(engine.state)
    engine.process_decisions()
    answer_decision(engine.state, decision["id"], "Rewrite the fixture")
    texts = [g["text"] for g in engine.state.guidance(work["id"])]
    assert any("Rewrite the fixture" in t and "override" in t for t in texts)


@pytest.mark.parametrize(
    "values",
    [
        {"confidence": "medium"},
        {"reversible": False},
        {"autonomy": "owner"},
        {"category": "other"},
    ],
)
def test_gate_requires_every_condition(tmp_path, values):
    engine, _ = run(tmp_path, auto_advice(**values))
    assert states(engine) == ["open"]


@pytest.mark.parametrize(
    "category",
    [
        "security",
        "credentials_spend",
        "acceptance_scope",
        "public_release",
        "product_ux",
    ],
)
def test_owner_only_categories_never_auto_apply(tmp_path, category):
    reply = auto_advice(category=category)
    parsed = Advice.model_validate(reply)
    assert auto_verdict(AutonomyPolicy(), parsed)[0] is False
    engine, _ = run(tmp_path, reply)
    assert states(engine) == ["open"]


def test_ask_all_mode_always_asks(tmp_path):
    engine, _ = run(
        tmp_path, auto_advice(), autonomy=AutonomyPolicy(mode="ask_all")
    )
    assert states(engine) == ["open"]


def test_upstream_category_needs_a_framework_option(tmp_path):
    framework = option(
        "Fix core",
        True,
        locus="framework:pluto-core",
        fix="Add scoped grants.",
        upstream=UPSTREAM_PAYLOAD,
    )
    reply = auto_advice(
        category="upstream_issue", options=[framework, option("Other")]
    )
    engine, _ = run(
        tmp_path,
        reply,
        framework=FrameworkPolicy(upstream={"pluto-core": UP}),
    )
    assert states(engine) == ["auto_pending"]
    # An app option cannot claim the upstream category.
    parsed = Advice.model_validate(auto_advice(category="upstream_issue"))
    assert auto_verdict(AutonomyPolicy(), parsed)[0] is False


def test_resume_after_fix_needs_a_factory_verified_fix():
    parsed = Advice.model_validate(auto_advice(category="resume_after_fix"))
    policy = AutonomyPolicy()
    assert auto_verdict(policy, parsed)[0] is False
    assert auto_verdict(policy, parsed, packet={"fix_verified": True})[0]


def test_config_change_is_bounded_by_max_config(tmp_path):
    change = {"key": "phase_timeout_seconds", "value": 3600}
    reply = auto_advice(category="config_change", config_change=change)
    policy = AutonomyPolicy(max_config={"phase_timeout_seconds": 7200})
    assert auto_verdict(policy, Advice.model_validate(reply))[0]
    too_big = auto_advice(
        category="config_change",
        config_change={**change, "value": 9000},
    )
    assert not auto_verdict(policy, Advice.model_validate(too_big))[0]
    unlisted = AutonomyPolicy(max_config={})
    assert not auto_verdict(unlisted, Advice.model_validate(reply))[0]
    engine, _ = run(tmp_path, reply, autonomy=policy)
    (decision,) = list_decisions(engine.state)
    assert decision["state"] == "auto_pending" and decision["kind"] == "config"
    apply_due_autos(engine.state, time.time() + 601)
    engine.process_decisions()
    assert engine.config.phase_timeout_seconds == 3600
