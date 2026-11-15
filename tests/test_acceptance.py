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


def gryt_decision():
    """gryt #9 event 6656: criterion 3 'passed' by owner decision, no refs."""
    refs = ["evidence-813f", "evidence-d3a4"]
    return Decision(
        action="advance",
        rationale="Advance as safe increment",
        acceptance=[
            assessment(0, "incomplete", refs),
            assessment(1, refs=refs + ["evidence-bdd0"]),
            assessment(2, refs=["evidence-bdd0"]),
            assessment(3, refs=[]),
        ],
    )


GRYT_CITABLE = {"evidence-813f", "evidence-d3a4", "evidence-bdd0"}


def test_gryt_empty_refs_error_names_criterion_and_citable_ids():
    from pluto_factory.acceptance import AcceptanceContractError

    with pytest.raises(AcceptanceContractError) as caught:
        acceptance_intent(gryt_decision(), 4, GRYT_CITABLE)
    assert (
        caught.value.problems == [caught.value.problems[0]]
        and "criterion 3" in caught.value.problems[0]
    )
    assert "evidence-bdd0" in str(caught.value)


def test_worker_check_ref_is_reported_as_not_citable():
    from pluto_factory.acceptance import AcceptanceContractError

    decision = Decision(
        action="advance",
        rationale="x",
        acceptance=[assessment(refs=["evidence-wc"])],
    )
    with pytest.raises(AcceptanceContractError, match="evidence-wc"):
        acceptance_intent(decision, 1, {"evidence-ok"})


def test_unverifiable_and_waived_allow_increment_without_refs():
    decision = Decision(
        action="advance",
        rationale="Safe increment",
        acceptance=[
            assessment(0, "unverifiable", []),
            assessment(1, "waived", []),
            assessment(2),
        ],
    )
    intent, remaining = acceptance_intent(decision, 3, {"current"})
    assert intent.action == "advance"
    assert remaining == ["Validated criterion"]


def test_unverifiable_cannot_complete():
    decision = Decision(
        action="complete",
        rationale="x",
        acceptance=[assessment(0, "unverifiable", [])],
    )
    with pytest.raises(ValueError, match="incomplete"):
        acceptance_intent(decision, 1, {"current"})
