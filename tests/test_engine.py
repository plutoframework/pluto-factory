import json

"""Full fixture work exercises real durable state, checks and Git integration."""

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from pluto_factory.config import digest, resolved_bundle
from pluto_factory.coordinator import Coordinator, CoordinatorError
from pluto_factory.demo import prepare_demo
from pluto_factory.engine import Engine
from pluto_factory.git import GitFailure
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
        def sync(self, entries, **_):
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
        self.closed = []

    def close(self, session):
        self.closed.append(session)

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
        assert harness.closed == [{"owned": True}]
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
        def sync(self, entries, **_):
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
        def sync(self, entries, **_):
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
        sync=lambda entries, **_: (entries, False),
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
        sync=lambda entries, **_: (entries, False),
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
        sync=lambda entries, **_: (entries, False),
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
    assert options.system_prompt.startswith("Role")
    assert "telegraphic" in options.system_prompt
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


@pytest.mark.parametrize("phase", ["qa", "qa_checks"])
def test_resumed_qa_synchronizes_candidate_with_advanced_default(
    tmp_path, phase
):
    home, prepared = prepare_demo(tmp_path / "advanced")
    engine = Engine(home)
    work_id = prepared["submitted"]["id"]
    try:
        work = engine.state.get_work(work_id)
        entries = engine.git.prepare(work_id, work["payload"]["repositories"])
        writable = next(
            entry for entry in entries if entry["access"] == "write"
        )
        (Path(writable["worktree"]) / "feature.txt").write_text("candidate\n")
        entries = engine.git.snapshot(entries, work_id)
        stale = {entry["name"]: entry["candidate"] for entry in entries}
        managed = Path(writable["managed"])
        (managed / "validation.txt").write_text("fixed on default\n")
        git(managed, "add", ".")
        git(
            managed,
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@localhost",
            "commit",
            "-m",
            "Fix validation on default",
        )
        advanced = git(managed, "rev-parse", "HEAD")
        work = engine.state.update_work(
            work_id,
            phase=phase,
            control="deferred",
            data={"entries": entries, "workspace_id": work_id},
        )
        engine.state.resume(work_id)
        engine._step(engine.state.get_work(work_id))
        current = engine.state.get_work(work_id)
        assert current["phase"] == "checking"
        assert current["current_run"] is None
        synced = next(
            entry
            for entry in current["data"]["entries"]
            if entry["name"] == writable["name"]
        )
        assert synced["base"] == advanced
        assert synced["candidate"] != stale[writable["name"]]
        assert any(
            event["kind"] == "candidate.synchronized"
            for event in engine.state.inspect()["events"]
        )
        assert not engine.state.inspect()["runs"]
    finally:
        engine.state.close()


def test_qa_dispatch_skips_sync_when_default_is_unchanged(tmp_path):
    home, prepared = prepare_demo(tmp_path / "unchanged")
    engine = Engine(home)
    work_id = prepared["submitted"]["id"]
    try:
        work = engine.state.get_work(work_id)
        entries = engine.git.prepare(work_id, work["payload"]["repositories"])
        entries = engine.git.snapshot(entries, work_id)
        engine.state.update_work(
            work_id,
            phase="qa_checks",
            data={"entries": entries, "workspace_id": work_id},
        )
        engine._step(engine.state.get_work(work_id))
        current = engine.state.get_work(work_id)
        assert current["phase"] == "qa"
        assert not any(
            event["kind"] == "candidate.synchronized"
            for event in engine.state.inspect()["events"]
        )
    finally:
        engine.state.close()


@pytest.mark.parametrize("outcome", ["revise", "defer"])
def test_implementer_handoff_decision_sees_committed_worktree_head(
    tmp_path, outcome
):
    home, prepared = prepare_demo(tmp_path / f"handoff-{outcome}")
    engine = Engine(home)
    work_id = prepared["submitted"]["id"]
    try:
        work = engine.state.get_work(work_id)
        entries = engine.git.prepare(work_id, work["payload"]["repositories"])
        writable = next(e for e in entries if e["access"] == "write")
        base = writable["base"]
        worktree = Path(writable["worktree"])
        (worktree / "feature.txt").write_text("implemented\n")
        git(worktree, "add", ".")
        git(
            worktree,
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@localhost",
            "commit",
            "-m",
            "Implement feature",
        )
        head = git(worktree, "rev-parse", "HEAD")
        assert head != base
        work = engine.state.update_work(
            work_id,
            phase="implementing",
            data={"entries": entries, "workspace_id": work_id},
        )
        run = engine.state.create_run(
            work_id,
            "implementer",
            {
                "id": f"run-{outcome}",
                "config_digest": digest(resolved_bundle(home, engine.config)),
            },
        )
        engine.state.update_run(
            run["id"], state="running", session={"owned": True}
        )
        engine.state.accept_report(
            run["id"],
            {
                "assignment_id": run["id"],
                "generation": run["generation"],
                "report": WorkerReport(
                    outcome=outcome,
                    summary="Committed work; policy blocker remains",
                    findings=[
                        {"severity": "required", "summary": "Policy blocker"}
                    ],
                ).model_dump(),
            },
        )
        seen = []

        class Judge:
            def decide(self, snapshot, allowed, prompt):
                seen.append(snapshot)
                return Decision(action="defer", rationale="Needs maintainer")

        engine.coordinator = Judge()
        engine.harness = ObservedHarness("settled")
        engine._report(
            engine.state.get_work(work_id), engine.state.get_run(run["id"])
        )
        decided = next(
            e for e in seen[0]["candidate"] if e["name"] == writable["name"]
        )
        assert decided["candidate"] == head
        assert "feature.txt" in decided["diff_stat"]
        assert any("Implement feature" in line for line in decided["commits"])
        stored = next(
            e
            for e in engine.state.get_work(work_id)["data"]["entries"]
            if e["name"] == writable["name"]
        )
        assert stored["candidate"] == head
    finally:
        engine.state.close()


def test_repeated_working_observations_append_one_event(tmp_path):
    home, prepared = prepare_demo(tmp_path / "observe-dedup")
    harness = ObservedHarness("working")
    engine = Engine(home, harness=harness)
    work_id = prepared["submitted"]["id"]
    engine.state.update_work(work_id, phase="implementing")
    run = engine.state.create_run(
        work_id,
        "implementer",
        {"id": "run-observed", "report_path": str(home / "absent.json")},
    )
    engine.state.update_run(run["id"], state="running", session={"owned": True})
    try:
        for _ in range(5):
            assert engine.tick()

        def observed():
            return [
                event["data"]
                for event in engine.state.inspect()["events"]
                if event["kind"] == "worker.observed"
            ]

        assert observed() == [{"run_id": run["id"], "state": "working"}]
        harness.current = "idle"
        engine.tick()
        assert [item["state"] for item in observed()] == ["working", "idle"]
    finally:
        engine.state.close()


def test_worker_check_runs_on_worktree_snapshot_and_records_evidence(tmp_path):
    from pluto_factory import claude_worker

    home, prepared = prepare_demo(tmp_path / "check")
    engine = Engine(home)
    work_id = prepared["submitted"]["id"]
    try:
        work = engine.state.get_work(work_id)
        entries = engine.git.prepare(work_id, work["payload"]["repositories"])
        writable = next(e for e in entries if e["access"] == "write")
        worktree = Path(writable["worktree"])
        (worktree / "wip.txt").write_text("uncommitted\n")
        before = git(worktree, "status", "--porcelain")
        engine.state.update_work(
            work_id, data={"entries": entries, "workspace_id": work_id}
        )
        folder = tmp_path / "assignment"
        run = engine.state.create_run(
            work_id,
            "implementer",
            {"id": "run-check", "assignment_path": str(folder / "a.json")},
        )
        assignment = engine.state.get_run(run["id"])["assignment"]
        (folder / "checks").mkdir(parents=True)
        request = "a" * 32
        (folder / "checks" / f"{request}.request.json").write_text("{}")
        engine._service_checks(
            engine.state.get_work(work_id), engine.state.get_run(run["id"])
        )
        for thread in list(engine._check_threads.values()):
            thread.join(30)
        answer = json.loads(
            (folder / "checks" / f"{request}.result.json").read_text()
        )
        assert claude_worker.pending_checks(assignment) == []
        assert answer["results"]
        assert git(worktree, "status", "--porcelain") == before
        rows = [
            json.loads(row["data"])
            for row in engine.state.db.execute(
                "SELECT data FROM evidence WHERE work_id=?", (work_id,)
            )
        ]
        assert rows and all(row["kind"] == "worker_check" for row in rows)
    finally:
        engine.state.close()


def test_coordinator_sees_verification_policy_against_deferral(tmp_path):
    home, prepared = prepare_demo(tmp_path / "policy")
    engine = Engine(home)
    try:
        seen = []

        class Judge:
            def decide(self, snapshot, allowed, prompt):
                seen.append(snapshot)
                return Decision(action="defer", rationale="x")

        engine.coordinator = Judge()
        work = engine.state.get_work(prepared["submitted"]["id"])
        engine._decide(work, {"defer", "advance"}, "advance")
        policy = seen[0]["verification_policy"]
        assert "not a reason to defer" in policy
        assert "genuine human decision" in policy
    finally:
        engine.state.close()


def _commit_default(managed, name, text):
    (managed / name).write_text(text)
    git(managed, "add", ".")
    git(
        managed,
        "-c",
        "user.name=t",
        "-c",
        "user.email=t@localhost",
        "commit",
        "-m",
        f"Change {name}",
    )


def _dispatch_implementer(tmp_path, advance, candidate_file=None):
    home, prepared = prepare_demo(tmp_path)
    engine = Engine(home)
    work_id = prepared["submitted"]["id"]
    launched = []
    engine._launch = launched.append
    work = engine.state.get_work(work_id)
    entries = engine.git.prepare(work_id, work["payload"]["repositories"])
    writable = next(e for e in entries if e["access"] == "write")
    if candidate_file:
        (Path(writable["worktree"]) / candidate_file[0]).write_text(
            candidate_file[1]
        )
        entries = engine.git.snapshot(entries, work_id)
    advance(Path(writable["managed"]))
    engine.state.update_work(
        work_id,
        phase="implementing",
        control="deferred",
        data={"entries": entries, "workspace_id": work_id},
    )
    engine.state.resume(work_id)
    engine._step(engine.state.get_work(work_id))
    packet = Path(launched[0]["assignment"]["packet_path"]).read_text()
    return engine, work_id, writable, json.loads(packet.split("\n\n", 1)[1])


def test_resumed_implementer_sees_merged_default(tmp_path):
    engine, work_id, writable, packet = _dispatch_implementer(
        tmp_path,
        lambda managed: _commit_default(managed, "fix.txt", "fixed\n"),
        ("feature.txt", "candidate\n"),
    )
    try:
        entry = next(
            e
            for e in engine.state.get_work(work_id)["data"]["entries"]
            if e["name"] == writable["name"]
        )
        assert (Path(entry["worktree"]) / "fix.txt").read_text() == "fixed\n"
        assert entry["base"] != writable["base"]
        assert any(
            e["kind"] == "candidate.synchronized"
            for e in engine.state.inspect()["events"]
        )
        sync = packet["default_branch_sync"]
        assert sync["default_branch_merged"] == [writable["name"]]
        assert "AGENTS.md" in sync["guidance"]
    finally:
        engine.state.close()


def test_implementer_dispatch_with_conflict_prepares_merge(tmp_path):
    engine, _work_id, writable, packet = _dispatch_implementer(
        tmp_path,
        lambda managed: _commit_default(managed, "shared.txt", "default\n"),
        ("shared.txt", "candidate\n"),
    )
    try:
        worktree = Path(writable["worktree"])
        assert "<<<<<<<" in (worktree / "shared.txt").read_text()
        assert (
            "conflicts in: shared.txt"
            in (packet["default_branch_sync"]["merge_conflict"])
        )
    finally:
        engine.state.close()


def test_implementer_dispatch_skips_sync_when_default_unchanged(tmp_path):
    engine, _, _, packet = _dispatch_implementer(tmp_path, lambda managed: None)
    try:
        assert packet["default_branch_sync"] is None
        assert not any(
            e["kind"] == "candidate.synchronized"
            for e in engine.state.inspect()["events"]
        )
    finally:
        engine.state.close()


def _gryt_engine(tmp_path, name, decide):
    """Integrating item with one citable checks row and one worker_check."""
    from types import SimpleNamespace

    home, prepared = prepare_demo(tmp_path / name)
    engine = Engine(home)
    work = integration_work(engine, prepared["submitted"]["id"])
    cfg = digest(resolved_bundle(engine.home, engine.config))
    candidates = {"core": "candidate-core", "cli": "candidate-cli"}
    for kind in ("checks", "worker_check"):
        engine.state.evidence(
            work["id"],
            {
                "kind": kind,
                "passed": True,
                "candidate_set": candidates,
                "config_digest": cfg,
            },
        )
    engine.coordinator = SimpleNamespace(decide=decide)
    engine.git = SimpleNamespace(
        sync=lambda entries, **_: (entries, False),
        validate_integration_plan=lambda entries: [],
    )
    engine.github = SimpleNamespace(
        adapter=SimpleNamespace(refresh_defaults=lambda entries: None),
        publish=lambda *a: None,
    )
    return engine, work


def _assessments(count, refs):
    from pluto_factory.models import CriterionAssessment

    return [
        CriterionAssessment(
            criterion=i, result="passed", evidence_refs=refs, explanation="e"
        )
        for i in range(count)
    ]


def _rejections(engine):
    rows = engine.state.db.execute(
        "SELECT data FROM events WHERE kind='coordinator.decision_rejected'"
    ).fetchall()
    return [json.loads(row["data"]) for row in rows]


def test_contract_invalid_decision_is_retried_with_precise_error(tmp_path):
    seen = []

    def decide(snapshot, allowed, prompt):
        seen.append(snapshot)
        citable = snapshot["citable_evidence_ids"]
        assert len(citable) == 1
        assert [r["citable"] for r in snapshot["evidence"]].count(False) == 1
        # gryt #9: first answer passes a criterion with no evidence refs.
        refs = [] if len(seen) == 1 else citable
        return Decision(
            action="advance",
            rationale="Safe increment",
            acceptance=_assessments(len(snapshot["work"]["acceptance"]), refs),
        )

    engine, work = _gryt_engine(tmp_path, "retry", decide)
    try:
        engine._integrate(work)
        current = engine.state.get_work(work["id"])
        assert current["control"] != "deferred"
        assert len(seen) == 2
        assert "contract_error" not in seen[0]
        error = seen[1]["contract_error"]
        assert "criterion 0" in error["problems"][0]
        assert error["citable_evidence_ids"] == seen[1]["citable_evidence_ids"]
        assert [r["will_retry"] for r in _rejections(engine)] == [True]
        assert current["data"]["acceptance_decision"]["action"] == "complete"
    finally:
        engine.state.close()


def test_worker_check_ref_is_rejected_then_corrected(tmp_path):
    seen = []

    def decide(snapshot, allowed, prompt):
        seen.append(snapshot)
        wrong = [r["id"] for r in snapshot["evidence"] if not r["citable"]]
        refs = wrong if len(seen) == 1 else snapshot["citable_evidence_ids"]
        return Decision(
            action="advance",
            rationale="x",
            acceptance=_assessments(len(snapshot["work"]["acceptance"]), refs),
        )

    engine, work = _gryt_engine(tmp_path, "workercheck", decide)
    try:
        engine._integrate(work)
        assert engine.state.get_work(work["id"])["control"] != "deferred"
        assert len(seen) == 2
        assert "not citable" in seen[1]["contract_error"]["problems"][0]
    finally:
        engine.state.close()


def test_persistently_invalid_decision_defers_after_recorded_retries(tmp_path):
    from pluto_factory.engine import CONTRACT_RETRIES

    seen = []

    def decide(snapshot, allowed, prompt):
        seen.append(snapshot)
        return Decision(
            action="advance",
            rationale="x",
            acceptance=_assessments(len(snapshot["work"]["acceptance"]), []),
        )

    engine, work = _gryt_engine(tmp_path, "persist", decide)
    try:
        with pytest.raises(ValueError, match="after 3 attempts"):
            engine._integrate(work)
        assert len(seen) == CONTRACT_RETRIES + 1
        rejections = _rejections(engine)
        assert [r["will_retry"] for r in rejections] == [True, True, False]
    finally:
        engine.state.close()


def test_unverifiable_criterion_advances_without_completing(tmp_path):
    from pluto_factory.models import CriterionAssessment

    def decide(snapshot, allowed, prompt):
        count = len(snapshot["work"]["acceptance"])
        items = _assessments(count, snapshot["citable_evidence_ids"])
        items[0] = CriterionAssessment(
            criterion=0,
            result="unverifiable",
            explanation="No configured check runs docker compose",
        )
        return Decision(action="advance", rationale="x", acceptance=items)

    engine, work = _gryt_engine(tmp_path, "unverifiable", decide)
    try:
        engine._integrate(work)
        recorded = engine.state.get_work(work["id"])["data"]
        assert recorded["acceptance_decision"]["action"] == "advance"
    finally:
        engine.state.close()


def test_interrupted_implementer_partial_edits_are_committed_and_resumed(
    tmp_path,
):
    home, prepared = prepare_demo(tmp_path)
    harness = ObservedHarness()
    engine = Engine(home, harness=harness)
    work_id = prepared["submitted"]["id"]
    launched = []
    engine._launch = launched.append
    work = engine.state.get_work(work_id)
    entries = engine.git.prepare(work_id, work["payload"]["repositories"])
    writable = next(e for e in entries if e["access"] == "write")
    engine.state.update_work(
        work_id,
        phase="implementing",
        control="ready",
        data={"entries": entries, "workspace_id": work_id},
    )
    run = engine.state.create_run(work_id, "implementer", {"id": "run-hung"})
    engine.state.update_run(run["id"], state="running", session={"owned": True})
    engine.state.update_work(work_id, current_run=run["id"])
    # The worker edits, then the phase deadline interrupts it mid-run.
    (Path(writable["worktree"]) / "partial.txt").write_text("half done\n")
    try:
        engine._failure(
            work_id,
            Failure(
                code="phase.timeout",
                summary="Assignment exceeded its phase deadline",
                source="coordinator",
            ),
        )
        assert harness.interrupted
        assert git(Path(writable["worktree"]), "status", "--porcelain")
        engine.state.update_work(work_id, control="ready", wake_at=0)
        engine._step(engine.state.get_work(work_id))
        # Recovery dispatches instead of looping on candidate_changed.
        assert not git(Path(writable["worktree"]), "status", "--porcelain")
        current = engine.state.get_work(work_id)
        entry = next(
            e
            for e in current["data"]["entries"]
            if e["name"] == writable["name"]
        )
        assert entry["candidate"] == git(
            Path(writable["worktree"]), "rev-parse", "HEAD"
        )
        assert entry["candidate"] != writable["candidate"]
        packet = Path(launched[0]["assignment"]["packet_path"]).read_text()
        notes = json.loads(packet.split("\n\n", 1)[1])["default_branch_sync"]
        assert notes["previous_run_interrupted"]["committed_partial_work"] == [
            writable["name"]
        ]
    finally:
        engine.state.close()


def test_candidate_changed_is_recovered_by_retry_not_coordinator(tmp_path):
    home, prepared = prepare_demo(tmp_path)
    engine = Engine(home)
    work_id = prepared["submitted"]["id"]
    engine._decide = lambda *a, **k: pytest.fail("coordinator consulted")
    try:
        engine._failure(
            work_id,
            Failure(
                code="git.candidate_changed",
                summary="Candidate has new changes",
                source="adapter",
            ),
        )
        current = engine.state.get_work(work_id)
        assert current["control"] == "backoff"
        assert current["recoveries"] == 1
    finally:
        engine.state.close()


def test_implementer_timeout_is_configured_separately(tmp_path):
    home, prepared = prepare_demo(tmp_path)
    engine = Engine(home)
    work_id = prepared["submitted"]["id"]
    run = engine.state.create_run(work_id, "implementer", {"id": "r"})
    engine.state.update_work(work_id, current_run=run["id"])
    work = engine.state.get_work(work_id)
    try:
        assert (
            engine._phase_timeout(work) == engine.config.phase_timeout_seconds
        )
        engine.config.implementer_phase_timeout_seconds = 5000
        assert engine._phase_timeout(work) == 5000
    finally:
        engine.state.close()


def _conflicted_work(tmp_path, phase, extra=None):
    home, prepared = prepare_demo(tmp_path)
    engine = Engine(home)
    work_id = prepared["submitted"]["id"]
    work = engine.state.get_work(work_id)
    entries = engine.git.prepare(work_id, work["payload"]["repositories"])
    writable = next(e for e in entries if e["access"] == "write")
    (Path(writable["worktree"]) / "shared.txt").write_text("candidate\n")
    entries = engine.git.snapshot(entries, work_id)
    _commit_default(Path(writable["managed"]), "shared.txt", "default\n")
    data = {
        "entries": entries,
        "workspace_id": work_id,
        "reports": {"reviewer": {"outcome": "ready"}},
        **(extra or {}),
    }
    engine.state.update_work(
        work_id, phase=phase, control="deferred", data=data
    )
    engine.state.resume(work_id)
    return engine, work_id, writable


@pytest.mark.parametrize(
    "phase", ["preparing", "checking", "qa_checks", "qa", "integrating"]
)
def test_sync_conflict_in_any_phase_revises_instead_of_deferring(
    tmp_path, phase
):
    engine, work_id, writable = _conflicted_work(tmp_path, phase)
    try:
        if phase == "integrating":
            current = digest(resolved_bundle(engine.home, engine.config))
            data = engine.state.get_work(work_id)["data"]
            data.update(
                checks_config_digest=current,
                qa_config_digest=current,
                report_config_digests={"reviewer": current, "qa": current},
            )
            engine.state.update_work(work_id, data=data)
        engine.tick()
        work = engine.state.get_work(work_id)
        assert work["phase"] == "implementing"
        assert work["control"] == "ready"
        assert work["revisions"] == 0
        data = work["data"]
        assert data["revision_reason"].startswith(
            "Resolve merge conflicts with the new default branch"
        )
        assert "shared.txt" in data["revision_reason"]
        assert data["conflict_revision"]["paths"] == ["shared.txt"]
        assert data["reports"]["reviewer"]["outcome"] == "ready"
        assert (
            "<<<<<<<" in (Path(writable["worktree"]) / "shared.txt").read_text()
        )
        assert not any(
            e["kind"] == "work.deferred"
            for e in engine.state.inspect()["events"]
        )
    finally:
        engine.state.close()


def test_conflict_revision_dispatches_implementer_with_merge_notes(tmp_path):
    engine, _work_id, _ = _conflicted_work(tmp_path, "qa_checks")
    launched = []
    engine._launch = launched.append
    try:
        engine.tick()
        engine.tick()
        packet = json.loads(
            Path(launched[0]["assignment"]["packet_path"])
            .read_text()
            .split("\n\n", 1)[1]
        )
        assert packet["conflict_revision"]["paths"] == ["shared.txt"]
        assert "shared.txt" in packet["default_branch_sync"]["merge_conflict"]
        assert "conflict-only" in packet["default_branch_sync"]["guidance"]
    finally:
        engine.state.close()


def _integrating_digests(engine, work_id):
    current = digest(resolved_bundle(engine.home, engine.config))
    data = engine.state.get_work(work_id)["data"]
    data.update(
        checks_config_digest=current,
        qa_config_digest=current,
        report_config_digests={"reviewer": current, "qa": current},
    )
    engine.state.update_work(work_id, data=data)


def test_resume_with_unresolved_merge_routes_to_conflict_revision(tmp_path):
    engine, work_id, _writable = _conflicted_work(tmp_path, "preparing")
    try:
        # A prior conflict left the merge prepared; the item was deferred.
        with pytest.raises(GitFailure):
            engine.git.sync(engine.state.get_work(work_id)["data"]["entries"])
        engine.state.update_work(
            work_id, phase="integrating", control="deferred"
        )
        _integrating_digests(engine, work_id)
        engine.state.resume(work_id)
        published = []
        engine.github = SimpleNamespace(
            adapter=SimpleNamespace(refresh_defaults=lambda _e: None),
            publish=lambda *a: published.append(a),
        )
        engine.tick()
        work = engine.state.get_work(work_id)
        assert not published
        assert work["phase"] == "implementing"
        assert work["data"]["conflict_revision"]["paths"] == ["shared.txt"]
    finally:
        engine.state.close()


def _awaiting(tmp_path, dirty_entries=True):
    engine, work_id, writable = _conflicted_work(tmp_path, "preparing")
    engine.state.update_work(work_id, phase="awaiting_merge", control="backoff")
    engine.github = SimpleNamespace(
        adapter=SimpleNamespace(refresh_defaults=lambda _e: None)
    )
    return engine, work_id, writable


STALE = [{"repository": "core", "merge_state": "DIRTY"}]


def test_dirty_pr_with_conflict_requests_conflict_revision(tmp_path):
    engine, work_id, _ = _awaiting(tmp_path)
    try:
        work = engine.state.get_work(work_id)
        with pytest.raises(GitFailure) as raised:
            engine._resync_pull_requests(work, STALE)
        assert engine._conflict_revision(work, raised.value)
        work = engine.state.get_work(work_id)
        assert work["phase"] == "implementing"
        assert work["data"]["pr_resyncs"] == 1
    finally:
        engine.state.close()


def test_dirty_pr_clean_sync_revalidates_and_is_bounded(tmp_path):
    engine, work_id, writable = _awaiting(tmp_path)
    try:
        managed = Path(writable["managed"])
        _commit_default(managed, "other.txt", "other\n")
        work = engine.state.get_work(work_id)
        # Resolve the earlier conflicting default change by using a new file
        # only: reset candidate to the original base first.
        subprocess.run(
            [
                "git",
                "-C",
                writable["worktree"],
                "reset",
                "--hard",
                "-q",
                work["data"]["entries"][0]["base"],
            ],
            check=True,
        )
        data = work["data"]
        data["entries"] = engine.git.snapshot(data["entries"], work_id)
        engine.state.update_work(work_id, data=data)
        engine.config.github_max_resyncs = 1
        work = engine.state.get_work(work_id)
        engine._resync_pull_requests(work, STALE)
        work = engine.state.get_work(work_id)
        assert work["phase"] == "checking"
        assert work["data"]["pr_resyncs"] == 1
        assert any(
            e["kind"] == "pull_requests.resynced"
            for e in engine.state.inspect()["events"]
        )
        engine.state.update_work(work_id, phase="awaiting_merge")
        engine._resync_pull_requests(engine.state.get_work(work_id), STALE)
        work = engine.state.get_work(work_id)
        assert work["control"] == "deferred"
    finally:
        engine.state.close()


def _resolve(writable, text="merged\n"):
    (Path(writable["worktree"]) / "shared.txt").write_text(text)


def _git_out(path, *args):
    return subprocess.run(
        ["git", "-C", str(path), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _awaiting_with_acceptance(tmp_path):
    engine, work_id, writable = _awaiting(tmp_path)
    data = engine.state.get_work(work_id)["data"]
    data.update(
        acceptance_decision={"action": "complete"},
        acceptance_candidate={"core": "74ce7a0"},
        acceptance_config_digest="old",
        publication_acceptance={"candidates": {"core": "74ce7a0"}},
        pull_requests=[{"repository": "core", "state": "open"}],
    )
    engine.state.update_work(work_id, data=data)
    return engine, work_id, writable


def test_conflict_revision_resets_acceptance_and_publication(tmp_path):
    engine, work_id, _ = _awaiting_with_acceptance(tmp_path)
    try:
        work = engine.state.get_work(work_id)
        with pytest.raises(GitFailure) as raised:
            engine._resync_pull_requests(work, STALE)
        assert engine._conflict_revision(work, raised.value)
        data = engine.state.get_work(work_id)["data"]
        for key in (
            "acceptance_decision",
            "acceptance_candidate",
            "acceptance_config_digest",
            "publication_acceptance",
        ):
            assert key not in data
    finally:
        engine.state.close()


def test_resolved_conflict_flows_to_checking_with_merge_commit(tmp_path):
    engine, work_id, writable = _awaiting_with_acceptance(tmp_path)
    try:
        work = engine.state.get_work(work_id)
        with pytest.raises(GitFailure) as raised:
            engine._resync_pull_requests(work, STALE)
        engine._conflict_revision(work, raised.value)
        old = engine.state.get_work(work_id)["data"]["entries"][0]["candidate"]
        # Worker resolves; factory hands off to checking.
        _resolve(writable)
        engine.state.update_work(work_id, phase="checking", control="ready")
        engine.tick()
        work = engine.state.get_work(work_id)
        entry = work["data"]["entries"][0]
        worktree = writable["worktree"]
        assert entry["candidate"] == _git_out(worktree, "rev-parse", "HEAD")
        assert entry["candidate"] != old
        assert len(_git_out(worktree, "rev-parse", "HEAD^@").split()) == 2
        assert work["phase"] == "reviewing"
        assert work["control"] != "deferred"
        assert "acceptance_candidate" not in work["data"]
        assert work["data"]["checks_candidate"] == {
            item["name"]: item["candidate"] for item in work["data"]["entries"]
        }
    finally:
        engine.state.close()


def test_second_conflict_after_resolution_keeps_merge_commit(tmp_path):
    engine, work_id, writable = _awaiting_with_acceptance(tmp_path)
    try:
        work = engine.state.get_work(work_id)
        with pytest.raises(GitFailure) as raised:
            engine._resync_pull_requests(work, STALE)
        engine._conflict_revision(work, raised.value)
        _resolve(writable)
        # The default branch moves again while the worker resolved.
        _commit_default(Path(writable["managed"]), "shared.txt", "newer\n")
        engine.state.update_work(work_id, phase="checking", control="ready")
        engine.tick()
        work = engine.state.get_work(work_id)
        assert work["phase"] == "implementing"
        assert work["data"]["conflict_revision"]
        launched = []
        engine._launch = launched.append
        engine.tick()
        events = [e["kind"] for e in engine.state.inspect()["events"]]
        assert "failure.observed" not in events
        assert engine.state.get_work(work_id)["control"] != "deferred"
        assert launched
    finally:
        engine.state.close()
