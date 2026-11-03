import pytest

from pluto_factory.acceptance import acceptance_intent
from pluto_factory.models import CriterionAssessment, Decision, Failure
from pluto_factory.recovery import plan_recovery


def assessment(index=0, result="passed", refs=None):
    return CriterionAssessment(
        criterion=index,
        result=result,
        evidence_refs=["current"] if refs is None else refs,
        explanation="Product scope still incomplete"
        if result == "incomplete"
        else "Validated criterion",
    )


def test_all_passed_advance_becomes_terminal_intent():
    decision = Decision(
        action="advance",
        rationale="Factory must finalize merge",
        acceptance=[assessment()],
    )
    intent, remaining = acceptance_intent(decision, 1, {"current"})
    assert intent.action == "complete"
    assert remaining == []
    assert decision.action == "advance"


def test_only_incomplete_criteria_justify_an_increment():
    decision = Decision(
        action="advance",
        rationale="Safe disabled increment",
        acceptance=[assessment(), assessment(1, "incomplete", [])],
    )
    intent, remaining = acceptance_intent(decision, 2, {"current"})
    assert intent.action == "advance"
    assert remaining == ["Product scope still incomplete"]


@pytest.mark.parametrize(
    "assessments",
    [
        [],
        [assessment(1)],
        [assessment(), assessment()],
        [assessment(refs=[])],
        [assessment(refs=["stale"])],
        [assessment(result="failed")],
    ],
)
@pytest.mark.parametrize("action", ["advance", "complete"])
def test_invalid_acceptance_never_authorizes_publication(action, assessments):
    decision = Decision(
        action=action, rationale="Claimed success", acceptance=assessments
    )
    with pytest.raises(ValueError):
        acceptance_intent(decision, 1, {"current"})


def test_complete_cannot_hide_incomplete_product_scope():
    decision = Decision(
        action="complete",
        rationale="Claimed complete",
        acceptance=[assessment(result="incomplete", refs=[])],
    )
    with pytest.raises(ValueError, match="incomplete"):
        acceptance_intent(decision, 1, {"current"})


def test_empty_publication_is_not_a_blind_retry():
    recovery = plan_recovery(
        Failure(
            code="github.no_changes",
            summary="Reconcile prior integration",
            source="adapter",
        ),
        0,
        3,
        2,
    )
    assert recovery.action == "defer"
    assert recovery.notify == "action_required"
