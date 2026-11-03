"""Worker packets carry the owner's executable validation contract."""

import json
from pathlib import Path

import pytest

from pluto_factory.config import write_config
from pluto_factory.engine import Engine
from pluto_factory.models import FactoryConfig, Repository, Submission


@pytest.mark.parametrize("role", ["implementer", "reviewer", "qa"])
def test_worker_packet_contains_exact_scoped_validation_plan(tmp_path, role):
    worktree = tmp_path / "assigned worktree"
    worktree.mkdir()
    checks = [["node", "--test", "tests/acceptance.test.mjs"]]
    qa = [["node", "/tooling with spaces/browser-qa.mjs"]]
    flags = [{"NEW_BEHAVIOR": "0"}, {"NEW_BEHAVIOR": "1"}]
    config = FactoryConfig(
        name="packet-test",
        harness="fixture",
        coordinator="rules",
        isolation="cooperative",
        command_timeout_seconds=73,
        repositories=[
            Repository(
                name="core",
                path=str(worktree),
                checks=checks,
                qa=qa,
                flag_environments=flags,
            ),
            Repository(
                name="unassigned",
                path=str(tmp_path),
                checks=[["unrelated-check"]],
                qa=[["unrelated-qa"]],
            ),
        ],
    )
    home = tmp_path / "factory"
    write_config(home, config)
    engine = Engine(home)
    captured = []

    class CapturingHarness:
        def find_existing(self, assignment):
            return None

        def launch(self, assignment):
            captured.append(assignment)
            return {"kind": "test", "address": "owned-test"}

    engine.harness = CapturingHarness()
    try:
        work = engine.state.submit(
            Submission(
                title="Validate the assigned application",
                instruction="Implement behavior and use the declared checks",
                acceptance=["Configured checks and browser QA pass"],
                repositories=["core"],
            )
        )
        work = engine.state.update_work(
            work["id"],
            phase="implementing",
            data={
                "entries": [
                    {
                        "name": "core",
                        "worktree": str(worktree),
                        "candidate": "candidate-one",
                        "access": "write",
                    }
                ],
            },
        )
        engine._dispatch(work, role)
        packet_text = Path(captured[0]["packet_path"]).read_text()
        packet = json.loads(packet_text.split("\n\n", 1)[1])
        assert packet["validation_plan"] == [
            {
                "repository": "core",
                "working_directory": str(worktree),
                "candidate_revision": "candidate-one",
                "checks": checks,
                "qa": qa,
                "flag_environments": flags,
                "timeout_seconds": 73,
            }
        ]
        assert "unrelated-check" not in packet_text
        assert "unrelated-qa" not in packet_text
    finally:
        engine.state.close()
