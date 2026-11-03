import pytest

from pluto_factory.models import Failure, Submission
from pluto_factory.recovery import plan_recovery
from pluto_factory.state import State


def submission(**changes):
    return Submission.model_validate(
        {
            "title": "Repair the sample",
            "instruction": "Preserve deployability",
            "acceptance": ["Required checks pass"],
            "repositories": ["core"],
            "delivery_key": "delivery-one",
            **changes,
        }
    )


def run(state, work_id, run_id):
    return state.create_run(work_id, "implementer", {"id": run_id})


def report(run_id, generation, **changes):
    return {
        "assignment_id": run_id,
        "generation": generation,
        "report": {"outcome": "ready", "summary": "Implemented", **changes},
    }


def test_submission_replay_is_idempotent_and_collision_is_rejected(tmp_path):
    state = State(tmp_path)
    first = state.submit(submission())
    assert state.submit(submission())["id"] == first["id"]
    with pytest.raises(ValueError, match="different work"):
        state.submit(submission(instruction="Different scope"))
    assert len(state.list_work()) == 1


def test_reports_are_fenced_and_replay_does_not_change_result(tmp_path):
    state = State(tmp_path)
    work = state.submit(submission())
    first = run(state, work["id"], "first")
    state.update_run("first", state="failed")
    state.update_work(work["id"], current_run=None)
    second = run(state, work["id"], "second")
    assert (
        state.accept_report("first", report("first", first["generation"]))
        == "stale"
    )
    assert (
        state.accept_report("second", report("second", second["generation"]))
        == "accepted"
    )
    assert (
        state.accept_report("second", report("second", second["generation"]))
        == "duplicate"
    )
    with pytest.raises(ValueError, match="conflicting"):
        state.accept_report(
            "second", report("second", second["generation"], summary="Changed")
        )
    assert state.get_work(work["id"])["current_run"] == "second"


def test_result_identity_cannot_be_selected_by_worker(tmp_path):
    state = State(tmp_path)
    first = state.submit(submission())
    second = state.submit(submission(delivery_key="two"))
    run(state, first["id"], "first")
    run(state, second["id"], "second")
    assert state.accept_report("first", report("second", 1)) == "stale"
    assert state.get_run("first")["report"] is None


def test_work_and_notification_commit_together_and_survive_restart(tmp_path):
    state = State(tmp_path)
    work = state.submit(submission())
    state.update_work(
        work["id"],
        control="deferred",
        notice={
            "kind": "action_required",
            "message": "Missing product decision",
            "incident_key": "one",
        },
    )
    state.close()
    reopened = State(tmp_path)
    assert reopened.get_work(work["id"])["control"] == "deferred"
    assert (
        reopened.inspect()["notifications"][0]["message"]
        == "Missing product decision"
    )


def test_only_one_coordinator_can_own_factory(tmp_path):
    first, second = State(tmp_path), State(tmp_path)
    with (
        first.writer(),
        pytest.raises(RuntimeError, match="already running"),
        second.writer(),
    ):
        pytest.fail("second coordinator acquired the lock")


def test_schema_and_missing_dependency_fail_explicitly(tmp_path):
    state = State(tmp_path)
    with pytest.raises(ValueError, match="unknown work"):
        state.submit(submission(depends_on=["absent"]))
    state.db.execute("PRAGMA user_version=99")
    state.close()
    with pytest.raises(ValueError, match="schema version"):
        State(tmp_path)


@pytest.mark.parametrize(
    "code",
    [
        "provider.rate_limited",
        "harness.exited",
        "report.invalid",
        "unknown",
    ],
)
def test_unknown_and_recoverable_failures_retry_within_budget(code):
    failure = Failure(code=code, summary="Observed", source="adapter")
    assert plan_recovery(failure, 0, 3, 2).action == "retry"
    assert plan_recovery(failure, 0, 3, 2).delay == 2
    assert plan_recovery(failure, 3, 3, 2).action == "defer"


def test_uncertain_side_effects_require_reconciliation_and_storage_pauses():
    uncertain = Failure(
        code="transport.timeout",
        summary="Reply lost",
        source="adapter",
        uncertain=True,
    )
    assert plan_recovery(uncertain, 0, 3, 2).action == "reconcile"
    unavailable = Failure(
        code="storage.unavailable",
        summary="Disk full",
        source="coordinator",
    )
    assert plan_recovery(unavailable, 0, 3, 2).action == "pause_factory"


def test_report_rejects_unknown_fields_and_negative_retry_hint(tmp_path):
    state = State(tmp_path)
    work = state.submit(submission())
    run(state, work["id"], "worker")
    with pytest.raises(ValueError):
        state.accept_report(
            "worker", report("worker", 1, command="merge anything")
        )
    with pytest.raises(ValueError):
        state.accept_report(
            "worker", report("worker", 1, retry_after_seconds=-1)
        )


def test_concurrent_redelivery_returns_the_same_work_item(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    State(tmp_path).close()
    barrier = Barrier(2)

    def submit_once():
        state = State(tmp_path)
        try:
            barrier.wait(timeout=5)
            return state.submit(submission())["id"]
        finally:
            state.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(submit_once) for _ in range(2)]
        results = [future.result(timeout=10) for future in futures]
    assert results[0] == results[1]
    state = State(tmp_path)
    assert len(state.list_work()) == 1
    assert len(state.inspect()["events"]) == 1
    state.close()
