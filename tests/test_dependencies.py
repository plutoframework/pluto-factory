"""Symbolic issue prerequisites survive intake reordering and restarts."""

import json

import pytest

from pluto_factory.config import write_config
from pluto_factory.dependencies import DependencyError
from pluto_factory.github_flow import GitHubFlow
from pluto_factory.models import FactoryConfig, Repository, Submission
from pluto_factory.state import State


def setup(tmp_path):
    repositories = []
    for name in ("core", "cli"):
        path = tmp_path / name
        path.mkdir()
        repositories.append(
            Repository(name=name, path=str(path), github=f"owner/{name}")
        )
    config = FactoryConfig(
        name="dependencies",
        repositories=repositories,
        harness="fixture",
        coordinator="rules",
        isolation="cooperative",
    )
    home = tmp_path / "factory"
    write_config(home, config)
    return State(home), config


def issue(number, *, dependencies=None, **changes):
    return Submission(
        title=f"Issue {number}",
        instruction="Implement this issue",
        acceptance=["Required behavior passes"],
        repositories=["core"],
        source="github:owner/core",
        delivery_key=str(number),
        depends_on_sources=dependencies or [],
        **changes,
    )


def test_out_of_order_issue_dependencies_survive_restart(tmp_path):
    state, _ = setup(tmp_path)
    dependent = state.submit(issue(2, dependencies=["github:owner/core:1"]))
    status = state.dependency_status(dependent["id"])
    assert not status["ready"]
    assert status["missing_sources"] == ["github:owner/core:1"]
    state.close()
    state = State(tmp_path / "factory")
    predecessor = state.submit(issue(1))
    status = state.dependency_status(dependent["id"])
    assert status["missing_sources"] == []
    assert status["unfinished_work"] == [predecessor["id"]]
    assert not status["ready"]
    state.update_work(predecessor["id"], phase="completed", control="settled")
    assert state.dependency_status(dependent["id"])["ready"]
    state.close()


def test_unmerged_or_deferred_predecessor_blocks_only_dependents(tmp_path):
    state, _ = setup(tmp_path)
    predecessor = state.submit(issue(1))
    dependent = state.submit(issue(2, dependencies=["github:owner/core:1"]))
    independent = state.submit(issue(3))
    state.update_work(
        predecessor["id"], phase="awaiting_merge", control="backoff"
    )
    assert not state.dependency_status(dependent["id"])["ready"]
    assert state.dependency_status(independent["id"])["ready"]
    state.update_work(predecessor["id"], control="deferred")
    status = state.dependency_status(dependent["id"])
    assert status["blocked_work"] == [predecessor["id"]]
    assert not status["ready"]
    state.close()


def test_dependency_chain_requires_every_predecessor_completed(tmp_path):
    state, _ = setup(tmp_path)
    first = state.submit(issue(1))
    second = state.submit(issue(2, dependencies=["github:owner/core:1"]))
    third = state.submit(issue(3, dependencies=["github:owner/core:2"]))
    assert not state.dependency_status(third["id"])["ready"]
    state.update_work(first["id"], phase="completed", control="settled")
    assert state.dependency_status(second["id"])["ready"]
    assert not state.dependency_status(third["id"])["ready"]
    state.update_work(second["id"], phase="completed", control="settled")
    assert state.dependency_status(third["id"])["ready"]
    state.close()


@pytest.mark.parametrize(
    "references",
    [
        ["github:owner/core:1"],
        ["github:OWNER/CORE:01"],
    ],
)
def test_self_dependency_is_rejected_before_storage(tmp_path, references):
    state, _ = setup(tmp_path)
    with pytest.raises(DependencyError, match="cycle"):
        state.submit(issue(1, dependencies=references))
    assert state.list_work() == []
    state.close()


def test_cycle_is_rejected_when_later_issue_resolves_missing_reference(
    tmp_path,
):
    state, _ = setup(tmp_path)
    first = state.submit(issue(1, dependencies=["github:owner/core:2"]))
    with pytest.raises(DependencyError, match="cycle"):
        state.submit(issue(2, dependencies=["github:owner/core:1"]))
    assert len(state.list_work()) == 1
    assert state.dependency_status(first["id"])["missing_sources"] == [
        "github:owner/core:2"
    ]
    state.close()


def test_cycle_across_internal_and_symbolic_dependencies_is_rejected(tmp_path):
    state, _ = setup(tmp_path)
    first = state.submit(issue(1, dependencies=["github:owner/core:2"]))
    with pytest.raises(DependencyError, match="cycle"):
        state.submit(issue(2, depends_on=[first["id"]]))
    state.close()


def test_internal_work_id_dependencies_remain_supported(tmp_path):
    state, _ = setup(tmp_path)
    first = state.submit(issue(1))
    second = state.submit(
        Submission(
            title="CLI work",
            instruction="Wait for the prerequisite",
            acceptance=["Works"],
            repositories=["core"],
            depends_on=[first["id"]],
        )
    )
    assert not state.dependency_status(second["id"])["ready"]
    state.update_work(first["id"], phase="completed", control="settled")
    assert state.dependency_status(second["id"])["ready"]
    state.close()


def test_unregistered_symbolic_repository_is_rejected(tmp_path):
    state, _ = setup(tmp_path)
    with pytest.raises(DependencyError, match="outside factory scope"):
        state.submit(issue(1, dependencies=["github:other/private:9"]))
    assert state.list_work() == []
    state.close()


def test_case_and_number_normalization_deduplicates_source_identity(tmp_path):
    state, _ = setup(tmp_path)
    first = state.submit(
        issue(1).model_copy(
            update={"source": "github:OWNER/CORE", "delivery_key": "001"}
        )
    )
    again = state.submit(issue(1))
    assert first["id"] == again["id"]
    assert first["source_key"] == "github:owner/core:1"
    state.close()


def test_old_payload_without_new_optional_field_remains_idempotent(tmp_path):
    state, _ = setup(tmp_path)
    first = state.submit(issue(1))
    payload = first["payload"]
    payload.pop("depends_on_sources")
    with state.db:
        state.db.execute(
            "UPDATE work SET payload=? WHERE id=?",
            (json.dumps(payload), first["id"]),
        )
    assert state.submit(issue(1))["id"] == first["id"]
    state.close()


def test_legacy_cycle_is_reported_as_not_ready(tmp_path):
    state, _ = setup(tmp_path)
    first = state.submit(issue(1))
    second = state.submit(issue(2))
    for item, predecessor in ((first, second), (second, first)):
        payload = item["payload"]
        payload["depends_on_sources"] = [predecessor["source_key"]]
        with state.db:
            state.db.execute(
                "UPDATE work SET payload=? WHERE id=?",
                (json.dumps(payload), item["id"]),
            )
    status = state.dependency_status(first["id"])
    assert status["cycle"]
    assert not status["ready"]
    state.close()


def test_preexisting_mixed_case_source_resolves_without_rewriting_history(
    tmp_path,
):
    state, _ = setup(tmp_path)
    first = state.submit(issue(1))
    with state.db:
        state.db.execute(
            "UPDATE work SET source_key=? WHERE id=?",
            ("github:OWNER/CORE:001", first["id"]),
        )
    second = state.submit(issue(2, dependencies=["github:owner/core:1"]))
    assert state.dependency_status(second["id"])["unfinished_work"] == [
        first["id"]
    ]
    assert state.submit(issue(1))["id"] == first["id"]
    state.close()


def test_rejected_cycle_does_not_prevent_independent_issue_intake(tmp_path):
    from types import SimpleNamespace

    state, config = setup(tmp_path)
    flow = GitHubFlow(state, config)
    flow.adapter = SimpleNamespace(
        poll=lambda: [
            issue(1, dependencies=["github:owner/core:2"]),
            issue(2, dependencies=["github:owner/core:1"]),
            issue(3),
        ]
    )
    flow.poll(force=True)
    items = state.list_work()
    assert len(items) == 2
    independent = next(
        item for item in items if item["payload"]["delivery_key"] == "3"
    )
    assert state.dependency_status(independent["id"])["ready"]
    assert any(
        event["kind"] == "intake.issue_rejected"
        for event in state.inspect()["events"]
    )
    state.close()
