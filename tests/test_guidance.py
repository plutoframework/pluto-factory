"""Human guidance reaches deferred work; coordinator and workers see it."""

import json
from pathlib import Path

import pytest

from pluto_factory import cli
from pluto_factory.config import write_config
from pluto_factory.engine import Engine
from pluto_factory.models import FactoryConfig, Repository, Submission


@pytest.fixture
def engine(tmp_path):
    worktree = tmp_path / "wt"
    worktree.mkdir()
    config = FactoryConfig(
        name="guide",
        harness="fixture",
        coordinator="rules",
        isolation="cooperative",
        repositories=[Repository(name="core", path=str(worktree))],
    )
    home = tmp_path / "factory"
    write_config(home, config)
    engine = Engine(home)
    work = engine.state.submit(
        Submission(
            title="t",
            instruction="i",
            acceptance=["a"],
            repositories=["core"],
        )
    )
    engine.work_id = work["id"]
    engine.worktree = worktree
    yield engine
    engine.state.close()


def defer(engine):
    work = engine.state.get_work(engine.work_id)
    engine._defer(work, "needs a maintainer decision")


def test_resume_with_guidance_records_event_and_resolves_notice(engine):
    defer(engine)
    assert engine.state.get_work(engine.work_id)["control"] == "deferred"
    result = engine.resume(engine.work_id, "  use option (a)  ", "alice")
    assert result["guidance"] == 1
    work = engine.state.get_work(engine.work_id)
    assert work["control"] == "ready"
    (entry,) = engine.state.guidance(engine.work_id)
    assert entry["author"] == "alice" and entry["text"] == "use option (a)"
    snapshot = engine.state.inspect()
    assert [
        e["kind"] for e in snapshot["events"] if e["kind"] == "human.guidance"
    ]
    (notice,) = snapshot["notifications"]
    assert notice["state"] == "resolved"


def test_guidance_requires_work_and_text(engine):
    with pytest.raises(ValueError):
        engine.resume(None, "x")
    with pytest.raises(ValueError):
        engine.state.add_guidance(engine.work_id, "   ", "a")
    with pytest.raises(ValueError):
        engine.state.add_guidance("work-missing", "x", "a")


def test_coordinator_snapshot_and_packet_carry_guidance(engine):
    seen = []

    class Recorder:
        def decide(self, snapshot, allowed, prompt):
            seen.append(snapshot)
            from pluto_factory.models import Decision

            return Decision(action="advance", rationale="r")

    engine.coordinator = Recorder()
    engine.resume(engine.work_id, "approve the bootstrap op", "bob")
    work = engine.state.get_work(engine.work_id)
    engine._decide(work, {"advance", "defer"}, "advance")
    (guidance,) = seen[0]["human_guidance"]
    assert guidance["text"] == "approve the bootstrap op"
    assert "AUTHORITATIVE" in guidance["authority"]
    assert "never defer again" in seen[0]["verification_policy"]

    captured = []

    class Harness:
        def find_existing(self, assignment):
            return None

        def launch(self, assignment):
            captured.append(assignment)
            return {"kind": "test", "address": "x"}

    engine.harness = Harness()
    wt = str(engine.worktree)
    work = engine.state.update_work(
        engine.work_id,
        phase="implementing",
        data={
            "entries": [
                {
                    "name": "core",
                    "worktree": wt,
                    "candidate": "c",
                    "access": "write",
                }
            ]
        },
    )
    engine._dispatch(work, "implementer")
    packet = json.loads(
        Path(captured[0]["packet_path"]).read_text().split("\n\n", 1)[1]
    )
    assert packet["human_guidance"][0]["text"] == "approve the bootstrap op"


def test_cli_guidance_file(engine, tmp_path, capsys):
    defer(engine)
    engine.state.close()
    note = tmp_path / "answer.txt"
    note.write_text("do (b)\n")
    code = cli.main(
        [
            "resume",
            "--home",
            str(engine.home),
            "--work",
            engine.work_id,
            "--guidance-file",
            str(note),
        ]
    )
    assert code == 0
    assert json.loads(capsys.readouterr().out)["guidance"] == 1
    both = cli.main(
        [
            "resume",
            "--home",
            str(engine.home),
            "--work",
            engine.work_id,
            "--guidance",
            "x",
            "--guidance-file",
            str(note),
        ]
    )
    assert both == 1
    engine.state = type(engine.state)(engine.home)
