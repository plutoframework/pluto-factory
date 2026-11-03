import pytest

from pluto_factory.config import write_config
from pluto_factory.coordinator import Coordinator, CoordinatorError
from pluto_factory.engine import Engine
from pluto_factory.models import (
    Decision,
    FactoryConfig,
    Failure,
    Repository,
    Submission,
)
from pluto_factory.state import StateConflict


@pytest.fixture
def engine(tmp_path):
    home = tmp_path / "factory"
    write_config(
        home,
        FactoryConfig(
            name="test",
            harness="fixture",
            coordinator="rules",
            isolation="cooperative",
            retry_base_seconds=0,
            repositories=[Repository(name="core", path=str(tmp_path))],
        ),
    )
    value = Engine(home)
    value.state.submit(
        Submission(
            title="Task",
            instruction="Implement safely",
            acceptance=["Correct"],
            repositories=["core"],
        )
    )
    yield value
    value.state.close()


def test_owner_pause_during_model_decision_cannot_advance_work(engine):
    work = engine.state.list_work()[0]

    def decide(_snapshot, _allowed, _prompt):
        engine.state.pause(work["id"])
        return Decision(action="advance", rationale="Finished")

    engine.coordinator.decide = decide
    with pytest.raises(StateConflict):
        engine._decide(work, {"advance"}, "advance")
    with pytest.raises(StateConflict):
        engine.state.update_work(work["id"], control="ready")
    assert engine.state.get_work(work["id"])["control"] == "paused"


def test_unknown_failure_gets_bounded_diagnosis_and_does_not_pause_factory(
    engine,
):
    work = engine.state.list_work()[0]
    seen = []

    def decide(snapshot, allowed, _prompt):
        seen.append((snapshot, allowed))
        return Decision(action="retry", rationale="Inspect and retry safely")

    engine.coordinator.decide = decide
    engine._failure(
        work["id"],
        Failure(
            code="unexpected.native_error",
            summary="Unexpected outcome",
            source="adapter",
        ),
    )
    current = engine.state.get_work(work["id"])
    assert current["control"] == "backoff"
    assert current["recoveries"] == 1
    assert seen[0][0]["evaluation"] == "recovery"
    assert seen[0][1] == {"retry", "revise", "defer"}
    assert not engine.state.is_paused()


def test_exhausted_deadline_terminates_owned_worker_before_deferral(engine):
    work = engine.state.list_work()[0]
    run = engine.state.create_run(work["id"], "implementer", {"id": "worker"})
    engine.state.update_run(run["id"], session={"owned": True})
    engine.state.update_work(
        work["id"], recoveries=engine.config.max_recoveries
    )
    calls = []

    class Harness:
        def observe(self, _session, _assignment):
            return {"state": "working"}

        def interrupt(self, _session):
            calls.append("interrupt")
            return {"outcome": "still_working"}

        def close(self, _session):
            calls.append("close")

    engine.harness = Harness()
    engine._failure(
        work["id"],
        Failure(
            code="phase.timeout",
            summary="Deadline exceeded",
            source="coordinator",
        ),
    )
    assert calls == ["interrupt", "close"]
    current = engine.state.get_work(work["id"])
    assert current["control"] == "deferred"
    assert current["current_run"] is None


def test_provider_exception_is_normalized_without_echoing_secret(
    engine, monkeypatch
):
    import pydantic_ai

    class BrokenAgent:
        def __init__(self, *_args, **_kwargs):
            pass

        def run_sync(self, _request):
            raise RuntimeError("sensitive diagnostic example")

    monkeypatch.setattr(pydantic_ai, "Agent", BrokenAgent)
    config = engine.config.model_copy(update={"coordinator": "model"})
    core = Coordinator(engine.home, config)
    with pytest.raises(CoordinatorError) as raised:
        core.decide({}, {"advance"}, "Evaluate safely")
    assert raised.value.failure.code == "coordinator.failed"
    assert "sensitive" not in raised.value.failure.summary


def test_changed_prompts_invalidate_old_integration_evidence(engine):
    work = engine.state.list_work()[0]
    engine.state.update_work(
        work["id"],
        phase="integrating",
        data={
            "entries": [],
            "checks_config_digest": "old",
            "qa_config_digest": "old",
        },
    )
    (engine.home / "prompts" / "reviewer.md").write_text("New review criteria")
    engine._integrate(engine.state.get_work(work["id"]))
    assert engine.state.get_work(work["id"])["phase"] == "checking"


def test_required_qa_rules_cannot_be_silently_weakened(engine):
    from pluto_factory.config import resolved_bundle

    (engine.home / "qa" / "rules.toml").write_text("require_checks = false\n")
    with pytest.raises(ValueError, match="unsupported QA gate"):
        resolved_bundle(engine.home, engine.config)


def test_global_pause_prevents_assignment_creation(engine):
    work = engine.state.list_work()[0]
    engine.state.pause()
    with pytest.raises(StateConflict):
        engine.state.create_run(work["id"], "implementer", {"id": "new"})
    assert not engine.state.inspect()["runs"]


def test_queued_work_needs_validation_configuration_before_dispatch(engine):
    assert engine.tick()
    work = engine.state.list_work()[0]
    assert work["phase"] == "queued"
    assert work["control"] == "deferred"
    assert engine.state.inspect()["runs"] == []
