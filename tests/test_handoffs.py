"""Phase handoffs authorize the next gate, not evidence from a future phase."""

import pytest

from pluto_factory.config import digest, resolved_bundle, write_config
from pluto_factory.engine import Engine
from pluto_factory.models import (
    Decision,
    FactoryConfig,
    Repository,
    Submission,
    WorkerReport,
)
from pluto_factory.state import StateConflict


@pytest.fixture
def engine(tmp_path):
    home = tmp_path / "factory"
    write_config(
        home,
        FactoryConfig(
            name="handoff-test",
            harness="fixture",
            coordinator="rules",
            isolation="cooperative",
            repositories=[
                Repository(
                    name="core",
                    path=str(tmp_path),
                    checks=[["configured-check"]],
                    qa=[["configured-qa"]],
                )
            ],
        ),
    )
    value = Engine(home)
    yield value
    value.state.close()


def reported_work(engine, role="implementer", outcome="ready", findings=None):
    work = engine.state.submit(
        Submission(
            title="Implement the requested feature",
            instruction="Implement and validate the feature",
            acceptance=["The requested behavior works"],
            repositories=["core"],
        )
    )
    phases = {
        "implementer": "implementing",
        "reviewer": "reviewing",
        "qa": "qa",
    }
    candidate = "base" if role == "implementer" else "candidate"
    work = engine.state.update_work(
        work["id"],
        phase=phases[role],
        data={
            "entries": [
                {
                    "name": "core",
                    "base": "base",
                    "candidate": candidate,
                    "worktree": str(engine.home / "worktrees/core"),
                    "access": "write",
                }
            ],
        },
    )
    run = engine.state.create_run(
        work["id"],
        role,
        {
            "id": f"{role}-run",
            "config_digest": digest(
                resolved_bundle(engine.home, engine.config)
            ),
        },
    )
    engine.state.update_run(run["id"], state="running", session={"owned": True})
    report = WorkerReport(
        outcome=outcome,
        summary="Worker reports implementation and self-checks",
        reviewed_repositories=["core"],
        findings=findings or [],
    )
    engine.state.accept_report(
        run["id"],
        {
            "assignment_id": run["id"],
            "generation": run["generation"],
            "report": report.model_dump(),
        },
    )
    return engine.state.get_work(work["id"]), engine.state.get_run(run["id"])


def test_ready_implementer_advances_to_checks_without_future_evidence_verdict(
    engine,
):
    work, run = reported_work(engine)

    class PrematureEvidenceJudge:
        def decide(self, snapshot, allowed, prompt):
            raise AssertionError("Future-stage evidence must not gate snapshot")

    engine.coordinator = PrematureEvidenceJudge()
    engine._report(work, run)
    current = engine.state.get_work(work["id"])
    assert current["phase"] == "checking"
    assert current["current_run"] is None
    assert current["revisions"] == 0
    assert current["data"]["entries"][0]["candidate"] == "base"
    assert engine.state.inspect()["evidence"] == []


@pytest.mark.parametrize("action", ["revise", "defer"])
def test_model_handoff_rationale_reaches_worker_or_deferred_notice(
    engine, action
):
    work, run = reported_work(engine, role="reviewer")
    rationale = "The rollback scenario lacks a required assertion"

    class ExplainingJudge:
        def decide(self, snapshot, allowed, prompt):
            return Decision(action=action, rationale=rationale)

    engine.coordinator = ExplainingJudge()
    engine._report(work, run)
    current = engine.state.get_work(work["id"])
    if action == "revise":
        assert current["data"]["revision_reason"] == rationale
    else:
        notice = engine.state.inspect()["notifications"][-1]
        assert notice["message"] == rationale
    assert rationale != run["report"]["summary"]


def test_reviewer_handoff_sees_only_current_evidence_and_stage_meanings(engine):
    work, run = reported_work(engine, role="reviewer")
    config_digest = digest(resolved_bundle(engine.home, engine.config))
    current_id = engine.state.evidence(
        work["id"],
        {
            "kind": "checks",
            "passed": True,
            "candidate_set": {"core": "candidate"},
            "config_digest": config_digest,
        },
    )
    engine.state.evidence(
        work["id"],
        {
            "kind": "checks",
            "passed": True,
            "candidate_set": {"core": "old-candidate"},
            "config_digest": config_digest,
        },
    )
    engine.state.evidence(
        work["id"],
        {
            "kind": "checks",
            "passed": True,
            "candidate_set": {"core": "candidate"},
            "config_digest": "old-policy",
        },
    )
    snapshots = []

    class EvidenceJudge:
        def decide(self, snapshot, allowed, prompt):
            snapshots.append(snapshot)
            return Decision(
                action="advance", rationale="Proceed to independent QA"
            )

    engine.coordinator = EvidenceJudge()
    engine._report(work, run)
    snapshot = snapshots[0]
    assert snapshot["evaluation"] == "handoff"
    assert [evidence["id"] for evidence in snapshot["evidence"]] == [current_id]
    assert snapshot["handoff"]["next_phase"] == "preparing"
    assert "merge" in snapshot["handoff"]["action_meanings"]["advance"].lower()
    assert engine.state.get_work(work["id"])["phase"] == "preparing"


def test_ready_implementation_cannot_bypass_independent_integration_gates(
    engine,
):
    work, run = reported_work(engine)
    engine._report(work, run)

    class NoUncheckedIntegration:
        def sync(self, entries):
            raise AssertionError(
                "Unvalidated implementation reached integration"
            )

        def integrate(self, entries):
            raise AssertionError("Unvalidated implementation was merged")

    engine.git = NoUncheckedIntegration()
    engine._integrate(engine.state.get_work(work["id"]))
    current = engine.state.get_work(work["id"])
    assert current["phase"] == "checking"
    assert current["phase"] != "completed"
    assert engine.state.inspect()["evidence"] == []


def test_required_implementation_finding_still_requires_a_revision(engine):
    work, run = reported_work(
        engine,
        findings=[
            {
                "severity": "required",
                "summary": "A required edge case is broken",
            }
        ],
    )
    snapshots = []

    class RequiredCorrectionJudge:
        def decide(self, snapshot, allowed, prompt):
            snapshots.append(snapshot)
            assert allowed == {"revise", "defer"}
            return Decision(action="revise", rationale="Fix the edge case")

    engine.coordinator = RequiredCorrectionJudge()
    engine._report(work, run)
    current = engine.state.get_work(work["id"])
    assert snapshots[0]["evaluation"] == "handoff"
    assert current["phase"] == "implementing"
    assert current["revisions"] == 1
    assert current["data"]["revision_reason"] == "Fix the edge case"


def test_owner_pause_blocks_deterministic_implementation_handoff(engine):
    work, run = reported_work(engine)
    engine.state.pause(work["id"])
    with pytest.raises(StateConflict):
        engine._report(work, run)
    current = engine.state.get_work(work["id"])
    assert current["control"] == "paused"
    assert current["current_run"] == run["id"]
    assert engine.state.get_run(run["id"])["state"] == "report_received"
