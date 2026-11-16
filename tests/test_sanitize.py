"""Only structure rejects; every content-level problem is repaired."""

import copy
import json
import random

import pytest
from test_advisor import UPSTREAM_PAYLOAD, advice, build, option
from test_triage import SPEC, advice_block

from pluto_factory.advisor import Advice, Advisor
from pluto_factory.sanitize import sanitize_advice, sanitize_triage
from pluto_factory.triage import TriageResult

LONG = " ".join(["word"] * 90)
JUNK = [None, "", "   ", 7, [], {}, "x" * 5000, LONG, "a\nb\nc", True]


def framework(**values):
    fields = {
        "locus": "framework:pluto-rbac",
        "fix": "Support grants.",
        "upstream": dict(UPSTREAM_PAYLOAD),
        **values,
    }
    return option("Fix it", True, **fields)


def test_live_failure_empty_fix_on_a_framework_option():
    raw = advice(
        options=[
            framework(fix=""),
            option("Other"),
        ]
    )
    fixed, changes = sanitize_advice(raw)
    parsed = Advice.model_validate(fixed)
    assert parsed.options[0].locus == "framework:pluto-rbac"
    assert parsed.options[0].fix  # filled from the effect
    assert any("fix: filled from effect" in c for c in changes)


def test_framework_option_without_payload_is_downgraded():
    bare = option("Fix", True, locus="framework:x", fix="A fix.")
    fixed, changes = sanitize_advice(advice(options=[bare, option("B")]))
    parsed = Advice.model_validate(fixed)
    assert parsed.options[0].locus == "app" and parsed.options[0].fix == ""
    assert any("downgraded" in c for c in changes)


def test_unknown_enums_fall_back_to_the_owner_only_defaults():
    raw = advice(
        category="make-it-so",
        autonomy="sure",
        confidence="very",
        autonomy_reason="",
    )
    fixed, changes = sanitize_advice(raw)
    parsed = Advice.model_validate(fixed)
    assert parsed.category == "other" and parsed.autonomy == "owner"
    assert parsed.confidence == "low" and changes


def test_structural_violations_still_reject():
    one = advice(options=[option("Only", True)])
    two_recommended = advice(options=[option("A", True), option("B", True)])
    none_recommended = advice(options=[option("A"), option("B")])
    for bad in (one, two_recommended, none_recommended):
        with pytest.raises(ValueError):
            Advice.model_validate(sanitize_advice(bad)[0])
    with pytest.raises(ValueError):
        Advice.model_validate(sanitize_advice(advice(problem="  "))[0])


def mutate(rng, raw):
    """A plausible malformed model reply that keeps the structure valid."""
    raw = copy.deepcopy(raw)
    for _ in range(rng.randint(1, 6)):
        target = rng.choice(["top", "option"])
        if target == "top":
            key = rng.choice(
                [
                    "details",
                    "why",
                    "category",
                    "autonomy",
                    "autonomy_reason",
                    "confidence",
                    "reversible",
                    "config_change",
                ]
            )
            raw[key] = rng.choice(JUNK + ["security", "bogus", "auto", "high"])
        else:
            item = rng.choice(raw["options"])
            key = rng.choice(
                [
                    "label",
                    "effect",
                    "tradeoff",
                    "locus",
                    "fix",
                    "upstream",
                    "completes",
                ]
            )
            if key == "locus":
                item[key] = rng.choice(
                    ["app", "framework:lib", "framework:", "weird", None, ""]
                )
            elif key == "upstream":
                item[key] = rng.choice(
                    [
                        None,
                        {},
                        {"title": LONG},
                        {**UPSTREAM_PAYLOAD, "acceptance": []},
                        {**UPSTREAM_PAYLOAD, "title": "x" * 500},
                        dict(UPSTREAM_PAYLOAD),
                        "text",
                    ]
                )
            else:
                item[key] = rng.choice(JUNK)
    return raw


def test_fuzz_sanitizing_never_raises_when_structure_holds():
    rng = random.Random(1234)
    base = advice(
        options=[
            framework(),
            option("Other"),
            option("Third"),
        ]
    )
    for case in range(600):
        raw = mutate(rng, base)
        # Keep the structural invariants the fuzz must not break.
        raw["problem"] = rng.choice(["Real problem.", LONG])
        raw["options"][0]["recommended"] = True
        for other in raw["options"][1:]:
            other["recommended"] = False
        fixed, _changes = sanitize_advice(raw, pull_request=case % 5 == 0)
        parsed = Advice.model_validate(fixed)  # must not raise
        assert sum(o.recommended for o in parsed.options) == 1
        assert len({o.label for o in parsed.options}) == len(parsed.options)


def test_fuzz_triage_results_never_raise_when_structure_holds():
    rng = random.Random(99)
    for _ in range(300):
        spec = copy.deepcopy(SPEC["spec"])
        spec[rng.choice(["scope", "title", "locus", "depends"])] = rng.choice(
            JUNK + ["not a ref", ["bad", "#4"], "framework:", "x/y#3"]
        )
        spec["acceptance"] = rng.choice(
            [[LONG], ["ok", "", "  "], ["x"] * 14, ["fine"]]
        )
        if not spec["title"] or not str(spec["title"]).strip():
            spec["title"] = "Fine title"
        if not isinstance(spec["scope"], str) or not spec["scope"].strip():
            spec["scope"] = "A scope."
        fixed, _ = sanitize_triage({"outcome": "ACCEPT", "spec": spec})
        TriageResult.model_validate(fixed)
        block = mutate(rng, advice_block_options())
        block["problem"] = "A problem."
        fixed, _ = sanitize_triage({"outcome": "NEEDS_HUMAN", "advice": block})
        TriageResult.model_validate(fixed)
        reject = {
            "category": rng.choice(["bogus", "duplicate"]),
            "reason": LONG,
        }
        fixed, _ = sanitize_triage({"outcome": "REJECT", "rejection": reject})
        TriageResult.model_validate(fixed)


def advice_block_options():
    block = advice_block()
    for key in ("category", "autonomy", "confidence"):
        block.pop(key, None)
    return block


def test_advisor_reports_what_it_sanitized(tmp_path):
    engine, _ = build(tmp_path)
    raw = advice(options=[framework(fix=""), option("Other")], why="")
    advisor = Advisor(engine.home, engine.config, lambda p, q: raw)
    _result, source = advisor.advise({"block_reason": "x"})
    assert source == "model"
    assert any("why: filled" in c for c in advisor.last_sanitized)
    json.dumps(advisor.last_sanitized)  # event-safe


def test_engine_logs_a_sanitized_event(tmp_path):
    from test_advisor import make_work_with_issue

    engine, _ = build(tmp_path)
    work = make_work_with_issue(engine)
    engine._defer(engine.state.get_work(work["id"]), "x")
    engine.advisor.runner = lambda p, q: advice(why="", problem=LONG)
    engine.advise_pending()
    events = [
        e
        for e in engine.state.inspect()["events"]
        if e["kind"] == "advice.sanitized"
    ]
    data = events[-1]["data"]
    data = json.loads(data) if isinstance(data, str) else data
    assert any("problem: clipped" in c for c in data["changes"])
