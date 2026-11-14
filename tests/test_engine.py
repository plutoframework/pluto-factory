import json

"""Full fixture work exercises real durable state, checks and Git integration."""

import subprocess
from pathlib import Path

import pytest

from pluto_factory.config import digest, resolved_bundle
from pluto_factory.coordinator import Coordinator, CoordinatorError
from pluto_factory.demo import prepare_demo
from pluto_factory.engine import Engine
from pluto_factory.harness import HarnessError
from pluto_factory.models import Decision, Failure, WorkerReport
from pluto_factory.state import StateConflict


def git(path: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(path), *arguments],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


@pytest.fixture(scope="module")
def completed_demo(tmp_path_factory):
    directory = tmp_path_factory.mktemp("factory-complete") / "demo"
    home, prepared = prepare_demo(directory)
    engine = Engine(home)
    try:
        snapshot = engine.run(max_seconds=300)
    finally:
        engine.state.close()
    return home, prepared, snapshot


def test_two_repository_item_runs_all_required_phases(completed_demo):
    home, prepared, snapshot = completed_demo
    work = next(
        item
        for item in snapshot["work"]
        if item["id"] == prepared["submitted"]["id"]
    )
    assert work["phase"] == "completed"
    assert work["control"] == "settled"
    assert work["current_run"] is None
    assert {run["role"] for run in snapshot["runs"]} == {
        "implementer",
        "reviewer",
        "qa",
    }
    assert all(run["report"] for run in snapshot["runs"])
    evidence = [
        row["data"]
        for row in snapshot["evidence"]
        if row["data"].get("kind") in {"checks", "qa"}
        and "command" in row["data"]
    ]
    assert len(evidence) == 8
    assert all(row["passed"] for row in evidence)
    assert {row["kind"] for row in evidence} == {"checks", "qa"}
    assert {row["environment"]["PLUTO_DEMO_FEATURE"] for row in evidence} == {
        "0",
        "1",
    }
    for row in evidence:
        assert set(row["candidate_set"]) == {"core", "cli"}
        assert row["candidate_set"][row["repository"]] == row["revision"]
        assert Path(row["artifact"]).is_file()
    for entry in work["data"]["integration"]:
        managed = Path(entry["managed"])
        assert git(managed, "rev-parse", "HEAD") == entry["merged_revision"]
        assert "return left + right" in (managed / "calculator.py").read_text()
    assert home.joinpath("state.sqlite3").exists()
    compatibility = [
        row["data"]
        for row in snapshot["evidence"]
        if row["data"].get("kind") == "compatibility"
    ]
    assert compatibility
    assert all(row["passed"] for row in compatibility)
    assert work["data"]["acceptance_decision"]["action"] == "complete"
    assert len(work["data"]["acceptance_decision"]["acceptance"]) == 2


def test_worker_crash_is_recovered_without_losing_item(tmp_path):
    home, prepared = prepare_demo(tmp_path / "crash", "crash_once")
    engine = Engine(home)
    try:
        snapshot = engine.run(max_seconds=300)
    finally:
        engine.state.close()
    work = next(
        item
        for item in snapshot["work"]
        if item["id"] == prepared["submitted"]["id"]
    )
    assert work["phase"] == "completed"
    assert work["recoveries"] == 1
    implementers = [
        run for run in snapshot["runs"] if run["role"] == "implementer"
    ]
    assert len(implementers) == 2
    assert implementers[0]["state"] == "failed"
    assert implementers[1]["state"] == "settled"
    assert implementers[1]["generation"] > implementers[0]["generation"]
    assert all(
        Path(entry["worktree"]).exists() for entry in work["data"]["entries"]
    )
    failures = [
        event
        for event in snapshot["events"]
        if event["kind"] == "failure.observed"
    ]
    assert failures[0]["data"]["code"] == "harness.exited"
    assert any(
        notice["kind"] == "progress" for notice in snapshot["notifications"]
    )
    assert not any(
        notice["kind"] == "action_required"
        for notice in snapshot["notifications"]
    )


def test_example_work_does_not_modify_source_repositories(completed_demo):
    home, prepared, _ = completed_demo
    for name, revision in prepared["source_revisions"].items():
        source = home.parent / "sources" / name
        assert git(source, "rev-parse", "HEAD") == revision
        assert git(source, "status", "--porcelain") == ""
        assert "return left - right" in (source / "calculator.py").read_text()


def test_item_pause_during_model_decision_cannot_be_overwritten(tmp_path):
    home, prepared = prepare_demo(tmp_path / "pause")
    engine = Engine(home)
    work = engine.state.get_work(prepared["submitted"]["id"])

    class PausingCoordinator:
        def decide(self, snapshot, allowed, prompt):
            engine.state.pause(work["id"])
            return Decision(
                action="advance", rationale="Would otherwise proceed"
            )

    engine.coordinator = PausingCoordinator()
    try:
        with pytest.raises(StateConflict):
            engine._decide(work, {"advance"}, "advance")
        current = engine.state.get_work(work["id"])
        assert current["control"] == "paused"
        assert current["updated"] != work["updated"]
    finally:
        engine.state.close()


def test_pause_during_launch_reconciliation_prevents_new_worker(tmp_path):
    home, prepared = prepare_demo(tmp_path / "launch-pause")
    engine = Engine(home)
    work_id = prepared["submitted"]["id"]
    engine.state.update_work(work_id, phase="implementing")
    run = engine.state.create_run(
        work_id,
        "implementer",
        {
            "id": "run-paused-launch",
            "work_id": work_id,
        },
    )

    class PausingLookup:
        def find_existing(self, assignment):
            engine.state.pause(work_id)

        def launch(self, assignment):
            raise AssertionError("a paused assignment launched a new worker")

    engine.harness = PausingLookup()
    try:
        with pytest.raises(StateConflict):
            engine._launch(run)
        assert engine.state.get_work(work_id)["control"] == "paused"
    finally:
        engine.state.close()


def test_changed_validation_configuration_invalidates_integration(tmp_path):
    home, prepared = prepare_demo(tmp_path / "configuration")
    engine = Engine(home)
    current_digest = digest(resolved_bundle(home, engine.config))
    work = engine.state.update_work(
        prepared["submitted"]["id"],
        phase="integrating",
        data={
            "entries": [{"name": "core", "candidate": "same-candidate"}],
            "checks_candidate": {"core": "same-candidate"},
            "qa_candidate": {"core": "same-candidate"},
            "checks_config_digest": "previous-configuration",
            "qa_config_digest": current_digest,
            "report_config_digests": {
                "reviewer": current_digest,
                "qa": current_digest,
            },
        },
    )

    class NoIntegration:
        def sync(self, entries):
            raise AssertionError(
                "stale policy evidence reached Git integration"
            )

    engine.git = NoIntegration()
    try:
        engine._integrate(work)
        current = engine.state.get_work(work["id"])
        assert current["phase"] == "checking"
        assert any(
            event["kind"] == "validation.configuration_changed"
            for event in engine.state.inspect()["events"]
        )
    finally:
        engine.state.close()


class ObservedHarness:
    def __init__(self, state="working"):
        self.current = state
        self.interrupted = False

    def observe(self, session, assignment):
        return {"state": self.current}

    def interrupt(self, session):
        self.interrupted = True
        self.current = "settled"
        return {"outcome": "stopped"}


def test_exhausted_recovery_quiesces_live_worker_before_deferral(tmp_path):
    home, prepared = prepare_demo(tmp_path / "exhausted")
    harness = ObservedHarness()
    engine = Engine(home, harness=harness)
    engine.config.max_recoveries = 0
    work_id = prepared["submitted"]["id"]
    run = engine.state.create_run(
        work_id, "implementer", {"id": "run-exhausted"}
    )
    engine.state.update_run(run["id"], state="running", session={"owned": True})
    try:
        engine._failure(
            work_id,
            Failure(
                code="phase.timeout",
                summary="Deadline expired",
                source="coordinator",
            ),
        )
        current = engine.state.get_work(work_id)
        assert harness.interrupted
        assert current["control"] == "deferred"
        assert current["current_run"] is None
        assert engine.state.get_run(run["id"])["state"] == "failed"
    finally:
        engine.state.close()


def test_coordinator_failure_becomes_recoverable_state(tmp_path):
    home, prepared = prepare_demo(tmp_path / "model-error")

    class UnavailableCoordinator:
        def decide(self, snapshot, allowed, prompt):
            raise CoordinatorError(
                Failure(
                    code="coordinator.failed",
                    summary="Injected provider failure",
                    source="coordinator",
                )
            )

    engine = Engine(
        home,
        harness=ObservedHarness("settled"),
        coordinator=UnavailableCoordinator(),
    )
    work_id = prepared["submitted"]["id"]
    engine.state.update_work(
        work_id,
        phase="reviewing",
        data={
            "entries": [
                {"name": "core", "candidate": "candidate-core"},
                {"name": "cli", "candidate": "candidate-cli"},
            ]
        },
    )
    run = engine.state.create_run(
        work_id,
        "reviewer",
        {
            "id": "run-model-error",
            "report_path": str(home / "absent-report.json"),
            "config_digest": digest(resolved_bundle(home, engine.config)),
        },
    )
    engine.state.update_run(run["id"], state="running", session={"owned": True})
    engine.state.accept_report(
        run["id"],
        {
            "assignment_id": run["id"],
            "generation": run["generation"],
            "report": WorkerReport(
                outcome="ready",
                summary="Review completed",
                reviewed_repositories=["core", "cli"],
            ).model_dump(),
        },
    )
    try:
        assert engine.tick()
        current = engine.state.get_work(work_id)
        assert current["control"] == "backoff"
        assert current["recoveries"] == 1
        assert current["current_run"] == run["id"]
        assert any(
            event["kind"] == "failure.observed"
            and event["data"]["code"] == "coordinator.failed"
            for event in engine.state.inspect()["events"]
        )
    finally:
        engine.state.close()


def test_provider_initialization_failure_is_normalized(tmp_path, monkeypatch):
    home, _ = prepare_demo(tmp_path / "provider-init")
    engine = Engine(home)
    engine.config.coordinator = "model"
    coordinator = Coordinator(home, engine.config)

    class ProviderInitializationError(RuntimeError):
        status_code = 401

    def unavailable(*arguments, **keywords):
        raise ProviderInitializationError("sensitive provider diagnostic")

    monkeypatch.setattr("pydantic_ai.Agent", unavailable)
    try:
        with pytest.raises(CoordinatorError) as error:
            coordinator.decide({"preferred": "advance"}, {"advance"}, "Role")
        assert error.value.failure.code == "auth.required"
        assert (
            "sensitive provider diagnostic" not in error.value.failure.summary
        )
    finally:
        engine.state.close()


def test_failure_during_interruption_preserves_owned_assignment(tmp_path):
    home, prepared = prepare_demo(tmp_path / "interruption-error")

    class UnreachableHarness(ObservedHarness):
        def interrupt(self, session):
            raise HarnessError(
                "harness.unavailable",
                "Injected lost connection",
                uncertain=True,
            )

        def close(self, session):
            raise HarnessError(
                "harness.unavailable", "Still unreachable", uncertain=True
            )

    engine = Engine(home, harness=UnreachableHarness())
    engine.config.max_recoveries = 0
    work_id = prepared["submitted"]["id"]
    run = engine.state.create_run(
        work_id, "implementer", {"id": "run-interruption-error"}
    )
    engine.state.update_run(run["id"], state="running", session={"owned": True})
    try:
        engine._failure(
            work_id,
            Failure(
                code="phase.timeout",
                summary="Deadline expired",
                source="coordinator",
            ),
        )
        current = engine.state.get_work(work_id)
        assert current["control"] == "quarantined"
        assert current["current_run"] == run["id"]
        assert engine.state.get_run(run["id"])["state"] == "running"
    finally:
        engine.state.close()


def integration_work(engine, work_id):
    config_digest = digest(resolved_bundle(engine.home, engine.config))
    candidates = {"core": "candidate-core", "cli": "candidate-cli"}
    return engine.state.update_work(
        work_id,
        phase="integrating",
        data={
            "entries": [
                {"name": name, "candidate": revision}
                for name, revision in candidates.items()
            ],
            "checks_candidate": candidates,
            "qa_candidate": candidates,
            "checks_config_digest": config_digest,
            "qa_config_digest": config_digest,
            "report_config_digests": {
                "reviewer": config_digest,
                "qa": config_digest,
            },
            "reports": {
                "implementer": {"outcome": "ready", "remaining_work": []},
                "reviewer": {"outcome": "ready"},
                "qa": {"outcome": "ready"},
            },
        },
    )


def test_failed_intermediate_compatibility_prevents_any_merge(tmp_path):
    home, prepared = prepare_demo(tmp_path / "compatibility")
    engine = Engine(home)
    work = integration_work(engine, prepared["submitted"]["id"])

    class IncompatibleGit:
        def sync(self, entries):
            return entries, False

        def validate_integration_plan(self, entries):
            return [{"kind": "compatibility", "passed": False}]

        def integrate(self, entries):
            raise AssertionError("incompatible prefix reached merge")

    engine.git = IncompatibleGit()
    try:
        engine._integrate(work)
        current = engine.state.get_work(work["id"])
        assert current["phase"] == "implementing"
        assert current["revisions"] == 1
        assert "compatibility" in current["data"]["revision_reason"]
    finally:
        engine.state.close()


def test_completion_requires_each_acceptance_criterion(tmp_path):
    home, prepared = prepare_demo(tmp_path / "acceptance")

    class UnsupportedCompletion:
        def decide(self, snapshot, allowed, prompt):
            return Decision(
                action="complete", rationale="Unsubstantiated claim"
            )

    engine = Engine(home, coordinator=UnsupportedCompletion())
    work = integration_work(engine, prepared["submitted"]["id"])

    class NoMerge:
        def sync(self, entries):
            return entries, False

        def validate_integration_plan(self, entries):
            return []

        def integrate(self, entries):
            raise AssertionError("unsupported acceptance reached merge")

    engine.git = NoMerge()
    try:
        with pytest.raises(ValueError, match="acceptance assessment"):
            engine._integrate(work)
        assert engine.state.get_work(work["id"])["phase"] != "completed"
    finally:
        engine.state.close()


def test_admin_remaining_and_all_passed_advance_do_not_create_another_increment(
    tmp_path,
):
    from types import SimpleNamespace

    from pluto_factory.models import CriterionAssessment

    home, prepared = prepare_demo(tmp_path / "admin-remaining")
    engine = Engine(home)
    work = integration_work(engine, prepared["submitted"]["id"])
    data = work["data"]
    admin = "Coordinator to finalize already-staged merge; no code fixes remain"
    data["reports"]["implementer"]["remaining_work"] = [admin]
    work = engine.state.update_work(work["id"], data=data)
    evidence_id = engine.state.evidence(
        work["id"],
        {
            "passed": True,
            "candidate_set": data["checks_candidate"],
            "config_digest": data["checks_config_digest"],
            "kind": "checks",
        },
    )

    class FinalAcceptance:
        def decide(self, snapshot, allowed, prompt):
            assert snapshot["acceptance_policy"]["worker_remaining_work"] == [
                admin
            ]
            return Decision(
                action="advance",
                rationale="Publication remains with factory",
                acceptance=[
                    CriterionAssessment(
                        criterion=i,
                        result="passed",
                        evidence_refs=[evidence_id],
                        explanation="Current evidence satisfies product scope",
                    )
                    for i in range(len(work["payload"]["acceptance"]))
                ],
            )

    observed = []

    def publish(item, entries, remaining):
        persisted = engine.state.get_work(item["id"])["data"]
        observed.append((remaining, persisted))

    engine.coordinator = FinalAcceptance()
    engine.git = SimpleNamespace(
        sync=lambda entries: (entries, False),
        validate_integration_plan=lambda entries: [],
    )
    engine.github = SimpleNamespace(
        adapter=SimpleNamespace(refresh_defaults=lambda entries: None),
        publish=publish,
    )
    try:
        engine._integrate(work)
        remaining, persisted = observed[0]
        assert remaining == []
        assert persisted["acceptance_decision"]["action"] == "complete"
        assert persisted["acceptance_candidate"] == data["checks_candidate"]
        assert (
            persisted["acceptance_config_digest"]
            == data["checks_config_digest"]
        )
        assert "increment" not in persisted
        assert engine.state.get_work(work["id"])["phase"] != "completed"
        assert any(
            e["kind"] == "acceptance.intent_normalized"
            for e in engine.state.inspect()["events"]
        )
    finally:
        engine.state.close()


def test_no_changes_failure_defers_without_model_retry_or_worker_dispatch(
    tmp_path,
):
    home, prepared = prepare_demo(tmp_path / "no-change-recovery")

    class NoDiagnosis:
        def decide(self, *args):
            raise AssertionError(
                "Known empty publication must not request a blind model retry"
            )

    engine = Engine(home, coordinator=NoDiagnosis())
    try:
        engine._failure(
            prepared["submitted"]["id"],
            Failure(
                code="github.no_changes",
                summary="Reconcile already merged work",
                source="adapter",
            ),
        )
        item = engine.state.get_work(prepared["submitted"]["id"])
        assert item["control"] == "deferred"
        assert item["recoveries"] == 0
        assert item["current_run"] is None
        assert (
            engine.state.inspect()["notifications"][-1]["kind"]
            == "action_required"
        )
    finally:
        engine.state.close()


def test_acceptance_policy_change_during_decision_prevents_publication(
    tmp_path,
):
    from types import SimpleNamespace

    from pluto_factory.models import CriterionAssessment

    home, prepared = prepare_demo(tmp_path / "acceptance-policy-change")
    engine = Engine(home)
    work = integration_work(engine, prepared["submitted"]["id"])

    class ChangedPolicy:
        def decide(self, snapshot, allowed, prompt):
            path = home / "prompts" / "orchestrator.md"
            path.write_text(path.read_text() + "\nUpdated QA policy.\n")
            return Decision(
                action="advance",
                rationale="Safe incomplete increment",
                acceptance=[
                    CriterionAssessment(
                        criterion=i,
                        result="incomplete",
                        explanation="Product scope remains",
                    )
                    for i in range(len(work["payload"]["acceptance"]))
                ],
            )

    def forbidden(*args):
        raise AssertionError("Changed acceptance policy reached publication")

    engine.coordinator = ChangedPolicy()
    engine.git = SimpleNamespace(
        sync=lambda entries: (entries, False),
        validate_integration_plan=lambda entries: [],
        integrate=forbidden,
    )
    engine.github = SimpleNamespace(
        adapter=SimpleNamespace(refresh_defaults=lambda entries: None),
        publish=forbidden,
    )
    try:
        engine._integrate(work)
        current = engine.state.get_work(work["id"])
        assert current["phase"] == "checking"
        assert "acceptance_decision" not in current["data"]
    finally:
        engine.state.close()


@pytest.mark.parametrize("action", ["revise", "defer"])
def test_explicit_acceptance_veto_wins_over_all_passed_assessments(
    tmp_path, action
):
    from types import SimpleNamespace

    from pluto_factory.models import CriterionAssessment

    home, prepared = prepare_demo(tmp_path / action)
    engine = Engine(home)
    work = integration_work(engine, prepared["submitted"]["id"])

    class Veto:
        def decide(self, snapshot, allowed, prompt):
            return Decision(
                action=action,
                rationale="Authority or quality blocker",
                acceptance=[
                    CriterionAssessment(
                        criterion=i,
                        result="passed",
                        evidence_refs=["claimed-evidence"],
                        explanation="Product criterion passes",
                    )
                    for i in range(len(work["payload"]["acceptance"]))
                ],
            )

    def forbidden(*args):
        raise AssertionError("Explicit acceptance veto reached publication")

    engine.coordinator = Veto()
    engine.git = SimpleNamespace(
        sync=lambda entries: (entries, False),
        validate_integration_plan=lambda entries: [],
        integrate=forbidden,
    )
    try:
        engine._integrate(work)
        current = engine.state.get_work(work["id"])
        assert (
            current["control"] == "deferred"
            if action == "defer"
            else current["phase"] == "implementing"
        )
        assert "acceptance_decision" not in current["data"]
    finally:
        engine.state.close()


def _claude_coordinator(tmp_path, monkeypatch, result):
    import claude_agent_sdk

    home, _ = prepare_demo(tmp_path / "claude-coordinator")
    engine = Engine(home)
    engine.state.close()
    engine.config.coordinator = "model"
    engine.config.coordinator_runtime = "claude_agent_sdk"
    engine.config.coordinator_model = "claude-sonnet-5-5"
    seen = {}

    async def query(prompt, options):
        seen.update(prompt=prompt, options=options)
        yield result

    monkeypatch.setattr(claude_agent_sdk, "query", query)
    return Coordinator(home, engine.config), seen


def _result(**overrides):
    from claude_agent_sdk import ResultMessage

    values = {
        "subtype": "success",
        "duration_ms": 1,
        "duration_api_ms": 1,
        "is_error": False,
        "num_turns": 1,
        "session_id": "s",
    }
    return ResultMessage(**{**values, **overrides})


def test_claude_agent_sdk_coordinator_decides(tmp_path, monkeypatch):
    coordinator, seen = _claude_coordinator(
        tmp_path,
        monkeypatch,
        _result(structured_output={"action": "advance", "rationale": "ok"}),
    )
    decision = coordinator.decide({"x": 1}, {"advance", "retry"}, "Role")
    assert decision.action == "advance"
    options = seen["options"]
    assert options.model == "claude-sonnet-5-5"
    assert options.system_prompt == "Role"
    assert options.tools == [] and options.setting_sources == []
    assert options.output_format["type"] == "json_schema"
    assert json.loads(seen["prompt"])["allowed_actions"] == [
        "advance",
        "retry",
    ]


def test_claude_agent_sdk_coordinator_normalizes_failure(tmp_path, monkeypatch):
    coordinator, _ = _claude_coordinator(
        tmp_path,
        monkeypatch,
        _result(is_error=True, api_error_status=429),
    )
    with pytest.raises(CoordinatorError) as error:
        coordinator.decide({}, {"advance"}, "Role")
    assert error.value.failure.code == "provider.rate_limited"
