import copy
from types import SimpleNamespace

import pytest

from pluto_factory.acceptance import current_evidence
from pluto_factory.config import digest, resolved_bundle, write_config
from pluto_factory.coordinator import CoordinatorError
from pluto_factory.engine import Engine
from pluto_factory.models import (
    CriterionAssessment,
    Decision,
    FactoryConfig,
    Failure,
    Repository,
    Submission,
)
from pluto_factory.post_merge import confirm_merged, reassess_merged
from pluto_factory.state import StateConflict

SHA = "1" * 40


@pytest.fixture
def merged(tmp_path):
    config = FactoryConfig(
        name="merged",
        coordinator="rules",
        harness="fixture",
        isolation="cooperative",
        integration="github",
        github_poll_seconds=1,
        github_required_checks=["validate"],
        close_source_issues=True,
        repositories=[
            Repository(name="core", path=str(tmp_path), github="owner/core")
        ],
    )
    home = tmp_path / "factory"
    write_config(home, config)
    engine = Engine(home)
    work = engine.state.submit(
        Submission(
            title="Acceptance",
            instruction="Finish app",
            repositories=["core"],
            acceptance=["Product works", "CI is green"],
            source="github:owner/core",
            delivery_key="9",
        )
    )
    revision = digest(resolved_bundle(home, engine.config))
    candidates = {"core": SHA}
    evidence = engine.state.evidence(
        work["id"],
        {
            "passed": True,
            "candidate_set": candidates,
            "config_digest": revision,
            "kind": "checks",
        },
    )
    decision = Decision(
        action="advance",
        rationale="CI is not available before publication",
        acceptance=[
            CriterionAssessment(
                criterion=0,
                result="passed",
                evidence_refs=[evidence],
                explanation="Product validated",
            ),
            CriterionAssessment(
                criterion=1, result="incomplete", explanation="Await hosted CI"
            ),
        ],
    )
    pr = {
        "repository": "core",
        "slug": "owner/core",
        "number": 17,
        "url": "https://github.com/owner/core/pull/17",
        "branch": "factory/accepted",
        "base": "main",
        "head": SHA,
        "observed_head": SHA,
        "state": "merged",
        "head_changed": False,
        "checks": [
            {"name": "validate", "status": "COMPLETED", "conclusion": "SUCCESS"}
        ],
    }
    data = {
        "entries": [
            {
                "name": "core",
                "candidate": SHA,
                "access": "write",
                "branch": pr["branch"],
                "default_branch": "main",
            }
        ],
        "pull_requests": [copy.deepcopy(pr)],
        "acceptance_decision": decision.model_dump(),
        "remaining_after_merge": ["Await hosted CI"],
        "reports": {
            "reviewer": {"outcome": "ready"},
            "qa": {"outcome": "ready"},
        },
        "report_config_digests": {"reviewer": revision, "qa": revision},
    }
    for kind in ("acceptance", "checks", "qa"):
        data[kind + "_candidate"] = candidates
        data[kind + "_config_digest"] = revision
    work = engine.state.update_work(
        work["id"], phase="awaiting_merge", data=data
    )
    closed = []
    remote = SimpleNamespace(pr=pr)

    def poll(_):
        return {"state": "merged", "pull_requests": [copy.deepcopy(remote.pr)]}

    engine.github.adapter = SimpleNamespace(
        poll_pull_requests=poll,
        refresh_defaults=lambda _: None,
        complete_sources=lambda item: (
            closed.append(item["id"]) or {"state": "closed"}
        ),
    )
    yield engine, work, remote, closed
    engine.state.close()


def advance(engine, work):
    engine.github.advance(
        work,
        engine._revise,
        engine._defer,
        reassess=lambda item: reassess_merged(engine, item),
        confirm=lambda item: confirm_merged(engine, item),
    )
    return engine.state.get_work(work["id"])


def model(action="complete", effect=None, invalid=False):
    def decide(snapshot, allowed, prompt):
        assert snapshot["report"]["stage"] == "post_merge"
        if effect:
            effect(snapshot)
        refs = [item["id"] for item in snapshot["evidence"]]
        assessments = (
            []
            if invalid
            else [
                CriterionAssessment(
                    criterion=i,
                    result="incomplete"
                    if action == "advance" and i == 1
                    else "passed",
                    evidence_refs=[]
                    if action == "advance" and i == 1
                    else refs,
                    explanation="Actual product scope remains"
                    if action == "advance" and i == 1
                    else "Satisfied",
                )
                for i in range(2)
            ]
        )
        return Decision(
            action=action,
            rationale="Actual correction required"
            if action == "revise"
            else "Fresh post-merge assessment",
            acceptance=assessments,
        )

    return SimpleNamespace(decide=decide)


def test_ci_only_incomplete_finishes_without_another_coding_increment(merged):
    engine, work, _remote, closed = merged
    current = advance(engine, work)
    assert current["phase"] == "completed"
    assert closed == [work["id"]]
    assert "increment" not in current["data"]
    assert not engine.state.inspect()["runs"]
    assert current["data"]["acceptance_decision"]["action"] == "complete"


@pytest.mark.parametrize(
    "checks",
    [
        [{"name": "validate", "status": "IN_PROGRESS", "conclusion": ""}],
        [],
        [{"name": "validate", "status": "COMPLETED", "conclusion": "SKIPPED"}],
    ],
)
def test_pending_missing_or_nonpassing_required_checks_wait_without_worker(
    merged, checks
):
    engine, work, remote, closed = merged
    remote.pr["checks"] = checks
    current = advance(engine, work)
    assert (
        current["phase"] == "awaiting_merge" and current["control"] == "backoff"
    )
    assert not closed and not engine.state.inspect()["runs"]
    remote.pr["checks"] = [
        {"name": "validate", "status": "COMPLETED", "conclusion": "SUCCESS"}
    ]
    assert advance(engine, current)["phase"] == "completed"
    assert len(closed) == 1


def test_failed_required_ci_does_not_wait_forever_as_missing_or_start_a_worker(
    merged,
):
    engine, work, remote, closed = merged
    remote.pr["checks"][0]["conclusion"] = "FAILURE"
    current = advance(engine, work)
    assert current["control"] == "deferred"
    assert not closed and not engine.state.inspect()["runs"]


@pytest.mark.parametrize("invalid", [False, True])
def test_model_failure_stays_at_merged_candidate_instead_of_revising_branch(
    merged, invalid
):
    engine, work, _remote, closed = merged
    if invalid:
        engine.coordinator = model(invalid=True)
    else:

        def unavailable(*args):
            raise CoordinatorError(
                Failure(
                    code="coordinator.failed",
                    source="coordinator",
                    summary="Temporary failure",
                )
            )

        engine.coordinator = SimpleNamespace(decide=unavailable)
    current = advance(engine, work)
    assert (
        current["phase"] == "awaiting_merge" and current["control"] == "backoff"
    )
    assert (
        current["recoveries"] == 1
        and current["data"]["entries"][0]["branch"] == "factory/accepted"
    )
    assert not closed and not engine.state.inspect()["runs"]


def test_real_product_remaining_gets_one_fresh_increment(merged):
    engine, work, _remote, closed = merged
    engine.coordinator = model("advance")
    current = advance(engine, work)
    assert current["phase"] == "queued" and current["data"]["increment"] == 1
    assert current["data"]["revision_reason"] == "Actual product scope remains"
    assert not closed and "pull_requests" not in current["data"]


@pytest.mark.parametrize("exhausted", [False, True])
def test_corrective_revision_uses_fresh_namespace_and_respects_budget(
    merged, exhausted
):
    engine, work, _remote, closed = merged
    engine.coordinator = model("revise")
    if exhausted:
        work = engine.state.update_work(
            work["id"], revisions=engine.config.max_revisions
        )
    current = advance(engine, work)
    assert not closed
    if exhausted:
        assert (
            current["control"] == "deferred"
            and "increment" not in current["data"]
        )
    else:
        assert (
            current["phase"] == "queued" and current["data"]["increment"] == 1
        )
        assert current["revisions"] == 1


def test_pause_during_assessment_prevents_closure(merged):
    engine, work, _remote, closed = merged
    engine.coordinator = model(effect=lambda _: engine.state.pause(work["id"]))
    with pytest.raises(StateConflict):
        advance(engine, work)
    assert (
        engine.state.get_work(work["id"])["control"] == "paused" and not closed
    )


def test_policy_change_during_assessment_defers_without_closure(merged):
    engine, work, _remote, closed = merged

    def change(_):
        path = engine.home / "prompts" / "orchestrator.md"
        path.write_text(path.read_text() + "\nNew policy\n")

    engine.coordinator = model(effect=change)
    assert advance(engine, work)["control"] == "deferred"
    assert not closed


def test_ci_rerun_after_model_decision_blocks_closure(merged):
    engine, work, remote, closed = merged
    engine.coordinator = model(
        effect=lambda _: remote.pr["checks"][0].update(conclusion="FAILURE")
    )
    assert advance(engine, work)["control"] == "deferred"
    assert not closed


def test_old_green_evidence_is_not_current_after_new_pending_observation(
    merged,
):
    engine, work, _remote, _closed = merged
    work = reassess_merged(engine, work)
    assert any(
        row["kind"] == "github_ci"
        for row in current_evidence(
            engine.state, engine.home, engine.config, work
        )
    )
    data = work["data"]
    data["pull_requests"][0]["checks"][0].update(
        status="IN_PROGRESS", conclusion=""
    )
    work = engine.state.update_work(work["id"], data=data)
    assert reassess_merged(engine, work) is None
    current = engine.state.get_work(work["id"])
    assert not any(
        row["kind"] == "github_ci"
        for row in current_evidence(
            engine.state, engine.home, engine.config, current
        )
    )


def test_no_checks_required_does_not_fabricate_ci_execution_or_main_ci(merged):
    engine, work, remote, _closed = merged
    engine.config.github_required_checks = []
    remote.pr["checks"] = []
    data = work["data"]
    revision = digest(resolved_bundle(engine.home, engine.config))
    for kind in ("acceptance", "checks", "qa"):
        data[kind + "_config_digest"] = revision
    data["report_config_digests"] = {"reviewer": revision, "qa": revision}
    data["pull_requests"] = [copy.deepcopy(remote.pr)]
    work = engine.state.update_work(work["id"], data=data)
    engine.coordinator = model("advance")
    current = reassess_merged(engine, work)
    assert current is not None
    proofs = [
        row
        for row in current_evidence(
            engine.state, engine.home, engine.config, current
        )
        if row["kind"].startswith("github_")
    ]
    assert len(proofs) == 1 and proofs[0]["kind"] == "github_delivery"
    assert proofs[0]["ci_executed"] is False
    assert (
        "does not establish merge-commit or default-branch CI"
        in proofs[0]["scope"]
    )


def test_deleted_policy_during_assessment_does_not_kill_watcher(merged):
    engine, work, _remote, closed = merged
    engine.coordinator = model(
        effect=lambda _: (engine.home / "prompts" / "orchestrator.md").unlink()
    )
    current = advance(engine, work)
    assert current["control"] == "deferred" and not closed


@pytest.mark.parametrize("result", ["PENDING", "FAILURE"])
def test_corrective_revision_confirms_ci_before_queuing_or_consuming_budget(
    merged, result
):
    engine, work, remote, closed = merged

    def change(_):
        remote.pr["checks"][0].update(
            status="IN_PROGRESS" if result == "PENDING" else "COMPLETED",
            conclusion="" if result == "PENDING" else result,
        )

    engine.coordinator = model("revise", effect=change)
    current = advance(engine, work)
    assert current["phase"] == "awaiting_merge" and current["revisions"] == 0
    assert "increment" not in current["data"] and not closed


def test_unknown_postmerge_failure_never_diagnoses_into_merged_branch_revision(
    merged,
):
    engine, work, _remote, _closed = merged
    engine.coordinator = SimpleNamespace(
        decide=lambda *_: (_ for _ in ()).throw(
            AssertionError("No generic coding revision after merge")
        )
    )
    engine._failure(
        work["id"],
        Failure(
            code="github.unavailable",
            summary="Temporary observation failure",
            source="adapter",
        ),
    )
    current = engine.state.get_work(work["id"])
    assert (
        current["phase"] == "awaiting_merge" and current["control"] == "backoff"
    )
    assert current["revisions"] == 0


def test_green_then_pending_then_green_reassesses_without_deferral_or_worker(
    merged,
):
    engine, work, remote, closed = merged

    def rerun(_):
        remote.pr["checks"][0].update(status="IN_PROGRESS", conclusion="")

    engine.coordinator = model(effect=rerun)
    current = advance(engine, work)
    assert current["control"] == "backoff" and not closed
    remote.pr["checks"][0].update(status="COMPLETED", conclusion="SUCCESS")
    engine.coordinator = model()
    current = advance(engine, current)
    assert current["phase"] == "completed" and len(closed) == 1
    assert not engine.state.inspect()["runs"]


def default_ci(engine, remote):
    remote.pr["merge_commit"] = "2" * 40
    remote.main = {
        "state": "observed",
        "branch": "main",
        "head": "2" * 40,
        "checks": [
            {
                "name": "validate",
                "status": "COMPLETED",
                "conclusion": "SUCCESS",
                "head_sha": "2" * 40,
            }
        ],
        "runs": [
            {
                "id": 99,
                "head_sha": "2" * 40,
                "head_branch": "main",
                "event": "push",
                "conclusion": "success",
            }
        ],
    }
    engine.github.adapter.default_branch_checks = lambda _: copy.deepcopy(
        remote.main
    )


def test_default_branch_ci_is_recorded_at_merge_commit_not_pr_head(merged):
    engine, work, remote, closed = merged
    default_ci(engine, remote)
    current = advance(engine, work)
    assert current["phase"] == "completed" and closed
    proofs = [
        row
        for row in current_evidence(
            engine.state, engine.home, engine.config, current
        )
        if row["kind"] == "github_default_ci"
    ]
    assert len(proofs) == 1 and proofs[0]["revision"] == "2" * 40
    assert proofs[0]["candidate_set"] == {"core": SHA}


def test_pending_default_branch_ci_waits_after_pr_checks_pass(merged):
    engine, work, remote, closed = merged
    default_ci(engine, remote)
    remote.main["checks"][0].update(status="IN_PROGRESS", conclusion="")
    current = advance(engine, work)
    assert current["control"] == "backoff" and not closed
    remote.main["checks"][0].update(status="COMPLETED", conclusion="SUCCESS")
    assert advance(engine, current)["phase"] == "completed"
    assert not engine.state.inspect()["runs"]


def test_failed_default_branch_ci_blocks_closure_even_with_green_pr_ci(merged):
    engine, work, remote, closed = merged
    default_ci(engine, remote)
    remote.main["checks"][0]["conclusion"] = "FAILURE"
    assert advance(engine, work)["control"] == "deferred" and not closed


def test_default_branch_ci_rerun_during_model_decision_reassesses(merged):
    engine, work, remote, closed = merged
    default_ci(engine, remote)

    def rerun(_):
        remote.main["checks"][0].update(status="IN_PROGRESS", conclusion="")

    engine.coordinator = model(effect=rerun)
    current = advance(engine, work)
    assert current["control"] == "backoff" and not closed
    assert not any(
        row["kind"] == "github_default_ci"
        for row in current_evidence(
            engine.state, engine.home, engine.config, current
        )
    )
    remote.main["checks"][0].update(status="COMPLETED", conclusion="SUCCESS")
    engine.coordinator = model()
    assert advance(engine, current)["phase"] == "completed"


def test_new_merge_waits_briefly_for_main_workflow_discovery(merged):
    from datetime import UTC, datetime

    engine, work, remote, closed = merged
    default_ci(engine, remote)
    remote.main.update(state="not_found", checks=[])
    remote.pr["merged_at"] = datetime.now(UTC).isoformat()
    assert advance(engine, work)["control"] == "backoff" and not closed


def test_mismatched_main_ci_commit_cannot_prove_acceptance(merged):
    engine, work, remote, closed = merged
    default_ci(engine, remote)
    remote.main["head"] = "3" * 40
    assert advance(engine, work)["control"] == "deferred" and not closed


def test_main_ci_provider_failure_keeps_work_awaiting_merge(merged):
    from pluto_factory.github import GitHubFailure

    engine, work, _remote, closed = merged

    def unavailable(_):
        raise GitHubFailure("github.timeout", "Observation timed out")

    engine.github.adapter.default_branch_checks = unavailable
    assert engine.tick()
    current = engine.state.get_work(work["id"])
    assert (
        current["phase"] == "awaiting_merge" and current["control"] == "backoff"
    )
    assert not closed and not engine.state.inspect()["runs"]


def test_check_result_order_changes_do_not_restart_acceptance(merged):
    engine, work, remote, closed = merged
    remote.pr["checks"].append(
        {"name": "lint", "status": "COMPLETED", "conclusion": "SUCCESS"}
    )
    default_ci(engine, remote)
    remote.main["checks"].append(
        {
            "name": "format",
            "status": "COMPLETED",
            "conclusion": "SUCCESS",
            "head_sha": "2" * 40,
        }
    )

    def reorder(_):
        remote.pr["checks"].reverse()
        remote.main["checks"].reverse()

    engine.coordinator = model(effect=reorder)
    assert advance(engine, work)["phase"] == "completed"
    assert len(closed) == 1 and not engine.state.inspect()["runs"]
