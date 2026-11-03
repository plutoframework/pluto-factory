import sys

import pytest

from pluto_factory.config import write_config
from pluto_factory.improvements import activate, propose, rollback
from pluto_factory.models import FactoryConfig, Improvement, Repository
from pluto_factory.state import State


@pytest.fixture
def setup(tmp_path):
    config = FactoryConfig(
        name="test", repositories=[Repository(name="core", path=str(tmp_path))]
    )
    home = tmp_path / "factory"
    write_config(home, config)
    state = State(home)
    yield state, config
    state.close()


def test_proposal_does_not_mutate_active_prompt_and_owner_can_rollback(setup):
    state, config = setup
    target = state.home / "prompts" / "implementer.md"
    previous = target.read_text()
    proposal = propose(
        state,
        config,
        None,
        Improvement(
            area="implementer_prompt",
            reason="More accurate reports",
            proposed_content="Include evidence of unresolved scope.\n",
        ),
    )
    assert target.read_text() == previous
    assert activate(state, config, proposal)["status"] == "activated"
    assert "unresolved scope" in target.read_text()
    assert rollback(state, proposal)["status"] == "rolled_back"
    assert target.read_text() == previous


def test_policy_and_permission_files_are_not_improvement_targets(setup):
    state, config = setup
    proposal = propose(
        state,
        config,
        None,
        Improvement(
            area="../factory.toml",
            reason="Expand authority",
            proposed_content="anything",
        ),
    )
    with pytest.raises(ValueError, match="not available"):
        activate(state, config, proposal)


def test_automatic_activation_requires_passing_evaluation(setup):
    state, config = setup
    config.self_improvement.mode = "automatic"
    target = state.home / "prompts" / "implementer.md"
    previous = target.read_text()
    proposal = propose(
        state,
        config,
        None,
        Improvement(
            area="implementer_prompt",
            reason="Candidate",
            proposed_content="Candidate",
        ),
    )
    assert target.read_text() == previous
    assert state.inspect()["improvements"][0]["status"] == "proposed"
    config.self_improvement.evaluations = [
        [sys.executable, "-c", "raise SystemExit(1)"]
    ]
    # The owner changed policy; a fresh proposal must be evaluated against it.
    with pytest.raises(ValueError, match="configuration changed"):
        activate(state, config, proposal, automatic=True)
    second = propose(
        state,
        config,
        None,
        Improvement(
            area="implementer_prompt",
            reason="Candidate",
            proposed_content="Candidate",
        ),
    )
    assert second
    assert target.read_text() == previous
