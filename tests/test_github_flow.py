import time
from types import SimpleNamespace

from pluto_factory.config import write_config
from pluto_factory.engine import Engine
from pluto_factory.github_flow import GitHubFlow
from pluto_factory.models import FactoryConfig, Repository, Submission
from pluto_factory.state import State


def setup(tmp_path):
    config = FactoryConfig(
        name="test",
        harness="fixture",
        coordinator="rules",
        isolation="cooperative",
        integration="github",
        github_poll_seconds=1,
        repositories=[
            Repository(name="core", path=str(tmp_path), github="owner/core")
        ],
    )
    home = tmp_path / "factory"
    write_config(home, config)
    state = State(home)
    return home, state, config


def submission():
    return Submission(
        title="Issue",
        instruction="Implement issue",
        acceptance=["Checks pass"],
        repositories=["core"],
        source="github:owner/core",
        delivery_key="1",
    )


def test_repeated_issue_delivery_uses_one_durable_work_item(tmp_path):
    _, state, config = setup(tmp_path)
    flow = GitHubFlow(state, config)
    flow.adapter = SimpleNamespace(poll=lambda: [submission()])
    flow.poll(force=True)
    flow.poll(force=True)
    assert len(state.list_work()) == 1
    edited = submission().model_copy(update={"instruction": "Edited issue"})
    flow.adapter = SimpleNamespace(poll=lambda: [edited])
    flow.poll(force=True)
    assert state.list_work()[0]["payload"]["instruction"] == "Implement issue"
    assert state.inspect()["events"][-1]["kind"] == "intake.issue_changed"
    state.close()


def test_publication_waits_for_merge_and_changed_head_cannot_complete(tmp_path):
    _, state, config = setup(tmp_path)
    work = state.submit(submission())
    flow = GitHubFlow(state, config)
    prs = [
        {"url": "https://github.com/owner/core/pull/1", "head_changed": False}
    ]
    flow.adapter = SimpleNamespace(publish=lambda _work, _entries: prs)
    flow.publish(work, [], [])
    current = state.get_work(work["id"])
    assert current["phase"] == "awaiting_merge"
    assert current["control"] == "backoff"
    notices = state.inspect()["notifications"]
    assert notices[0]["kind"] == "progress"
    calls = []
    flow.adapter = SimpleNamespace(
        poll_pull_requests=lambda _prs: {
            "state": "merged",
            "pull_requests": [{**prs[0], "head_changed": True}],
        }
    )
    flow.advance(
        current,
        lambda *_: calls.append("revise"),
        lambda *_: calls.append("defer"),
    )
    assert calls == ["defer"]
    assert state.get_work(work["id"])["phase"] != "completed"
    state.close()


def test_all_confirmed_merges_close_issue_before_completing_work(tmp_path):
    _, state, config = setup(tmp_path)
    work = state.submit(submission())
    prs = [
        {"url": "https://github.com/owner/core/pull/1", "head_changed": False}
    ]
    current = state.update_work(
        work["id"],
        phase="awaiting_merge",
        data={
            "entries": [],
            "pull_requests": prs,
            "remaining_after_merge": [],
        },
    )
    flow = GitHubFlow(state, config)
    calls = []
    flow.adapter = SimpleNamespace(
        poll_pull_requests=lambda _: {"state": "merged", "pull_requests": prs},
        refresh_defaults=lambda _: calls.append("refresh"),
        complete_sources=lambda _: calls.append("close_issue"),
    )
    flow.advance(current, lambda *_: None, lambda *_: None)
    assert calls == ["refresh", "close_issue"]
    assert state.get_work(work["id"])["phase"] == "completed"
    state.close()


def test_watcher_stays_available_with_empty_queue(tmp_path):
    home, state, _ = setup(tmp_path)
    state.close()
    engine = Engine(home)
    calls = []
    engine.github = SimpleNamespace(poll=lambda **_: calls.append("poll"))
    started = time.monotonic()
    result = engine.run(max_seconds=0.02)
    assert calls == ["poll"]
    assert time.monotonic() - started >= 0.02
    assert result["work"] == []
    engine.state.close()
