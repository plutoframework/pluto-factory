import time
from types import SimpleNamespace

import pytest

from pluto_factory.config import digest, resolved_bundle, write_config
from pluto_factory.engine import Engine
from pluto_factory.github_flow import GitHubFlow
from pluto_factory.models import FactoryConfig, Repository, Submission
from pluto_factory.state import State, StateConflict


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


def merged_work(state, config, *, count=1, incomplete=False):
    item = submission().model_copy(
        update={
            "repositories": [repo.name for repo in config.repositories],
            "acceptance": [f"Criterion {index}" for index in range(count)],
        }
    )
    work = state.submit(item)
    current_digest = digest(resolved_bundle(state.home, config))
    entries = [
        {
            "name": repo.name,
            "access": repo.access,
            "candidate": str(index + 1) * 40,
            "branch": f"factory/work-{repo.name}",
            "default_branch": "main",
        }
        for index, repo in enumerate(config.repositories)
    ]
    candidates = {entry["name"]: entry["candidate"] for entry in entries}
    evidence_ids = [
        state.evidence(
            work["id"],
            {
                "kind": kind,
                "candidate_set": candidates,
                "config_digest": current_digest,
                "passed": True,
            },
        )
        for kind in ("checks", "qa")
    ]
    prs = [
        {
            "repository": entry["name"],
            "slug": repo.github,
            "number": index + 1,
            "url": f"https://github.com/{repo.github}/pull/{index + 1}",
            "head_changed": False,
            "head": entry["candidate"],
            "observed_head": entry["candidate"],
            "branch": entry["branch"],
            "base": entry["default_branch"],
            "state": "merged",
        }
        for index, (entry, repo) in enumerate(zip(entries, config.repositories))
        if repo.access == "write"
    ]
    return state.update_work(
        work["id"],
        phase="awaiting_merge",
        data={
            "entries": entries,
            "pull_requests": prs,
            "remaining_after_merge": ["Coordinator to finalize merge"],
            "acceptance_decision": {
                "action": "advance" if incomplete else "complete",
                "rationale": "Independent current acceptance assessment",
                "acceptance": [
                    {
                        "criterion": index,
                        "result": "incomplete"
                        if incomplete and index == count - 1
                        else "passed",
                        "evidence_refs": evidence_ids,
                        "explanation": "Implement the remaining criterion"
                        if incomplete and index == count - 1
                        else "Behavior is independently verified",
                    }
                    for index in range(count)
                ],
            },
            **{
                f"{kind}_candidate": candidates
                for kind in ("acceptance", "checks", "qa")
            },
            **{
                f"{kind}_config_digest": current_digest
                for kind in ("acceptance", "checks", "qa")
            },
            "report_config_digests": {
                role: current_digest for role in ("reviewer", "qa")
            },
            "reports": {
                role: {"outcome": "ready"} for role in ("reviewer", "qa")
            },
        },
    )


def merged_flow(state, config, work, calls, *, closure="closed"):
    flow = GitHubFlow(state, config)
    flow.adapter = SimpleNamespace(
        poll_pull_requests=lambda _: {
            "state": "merged",
            "pull_requests": work["data"]["pull_requests"],
        },
        refresh_defaults=lambda _: calls.append("refresh"),
        complete_sources=lambda _: (
            calls.append("close_issue") or {"state": closure}
        ),
    )
    return flow


def advance(flow, work, calls):
    flow.advance(
        work,
        lambda *_: calls.append("revise"),
        lambda current, reason: calls.append(("defer", current, reason)),
    )


def test_all_confirmed_merges_close_issue_before_completing_work(tmp_path):
    _, state, config = setup(tmp_path)
    work = merged_work(state, config)
    calls = []
    flow = merged_flow(state, config, work, calls)
    advance(flow, work, calls)
    assert calls == ["refresh", "close_issue"]
    assert state.get_work(work["id"])["phase"] == "completed"
    state.close()


@pytest.mark.parametrize(
    "field",
    [
        "acceptance_candidate",
        "acceptance_config_digest",
        "checks_candidate",
        "checks_config_digest",
        "qa_candidate",
        "qa_config_digest",
        "report_config_digests",
        "reports",
        "acceptance_decision",
    ],
)
def test_missing_postmerge_binding_defers_without_closure(tmp_path, field):
    _, state, config = setup(tmp_path)
    work = merged_work(state, config)
    work["data"].pop(field)
    calls = []
    advance(merged_flow(state, config, work, calls), work, calls)
    assert len(calls) == 1 and calls[0][0] == "defer"
    assert "reconciliation" in calls[0][2]
    current = state.get_work(work["id"])
    assert current["phase"] == "awaiting_merge"
    assert current["data"]["pull_requests"][0]["state"] == "merged"
    state.close()


@pytest.mark.parametrize("change", ["config", "candidate", "evidence"])
def test_stale_postmerge_acceptance_defers(tmp_path, change):
    _, state, config = setup(tmp_path)
    work = merged_work(state, config)
    if change == "config":
        (state.home / "prompts" / "orchestrator.md").write_text("New policy")
    elif change == "candidate":
        work["data"]["acceptance_candidate"] = {"core": "f" * 40}
    else:
        with state.db:
            state.db.execute(
                "DELETE FROM evidence WHERE work_id=?", (work["id"],)
            )
    calls = []
    advance(merged_flow(state, config, work, calls), work, calls)
    assert len(calls) == 1 and calls[0][0] == "defer"
    assert state.get_work(work["id"])["phase"] == "awaiting_merge"
    state.close()


@pytest.mark.parametrize(
    "mutation",
    ["missing", "duplicate", "head", "observed_head", "branch", "base", "slug"],
)
def test_merged_pr_requires_exact_validated_repository_provenance(
    tmp_path, mutation
):
    _, state, config = setup(tmp_path)
    work = merged_work(state, config)
    prs = work["data"]["pull_requests"]
    if mutation == "missing":
        prs.clear()
    elif mutation == "duplicate":
        prs.append(dict(prs[0]))
    else:
        prs[0][mutation] = "unrelated"
    calls = []
    advance(merged_flow(state, config, work, calls), work, calls)
    assert len(calls) == 1 and calls[0][0] == "defer"
    assert state.get_work(work["id"])["phase"] == "awaiting_merge"
    state.close()


def test_legacy_advance_with_all_passed_does_not_autoqueue_or_close(tmp_path):
    _, state, config = setup(tmp_path)
    work = merged_work(state, config)
    work["data"]["acceptance_decision"]["action"] = "advance"
    calls = []
    advance(merged_flow(state, config, work, calls), work, calls)
    assert len(calls) == 1 and calls[0][0] == "defer"
    assert "terminal acceptance" in calls[0][2]
    assert state.get_work(work["id"])["phase"] == "awaiting_merge"
    state.close()


def test_increment_continuation_uses_only_criteria_and_preserves_merged_prs(
    tmp_path,
):
    _, state, config = setup(tmp_path)
    work = merged_work(state, config, count=2, incomplete=True)
    original_prs = work["data"]["pull_requests"]
    calls = []
    advance(merged_flow(state, config, work, calls), work, calls)
    assert calls == ["refresh"]
    current = state.get_work(work["id"])
    assert current["phase"] == "queued"
    assert current["data"]["increment"] == 1
    assert (
        "Implement the remaining criterion"
        in current["data"]["revision_reason"]
    )
    assert "Coordinator to finalize" not in current["data"]["revision_reason"]
    assert (
        current["data"]["merged_increments"][0]["pull_requests"] == original_prs
    )
    assert "pull_requests" not in current["data"]
    state.close()


def test_partial_multi_repository_merge_keeps_waiting(tmp_path):
    _, state, config = setup(tmp_path)
    config.repositories.append(
        Repository(name="cli", path=str(tmp_path / "cli"), github="owner/cli")
    )
    work = merged_work(state, config)
    prs = work["data"]["pull_requests"]
    prs[1]["state"] = "open"
    calls = []
    flow = merged_flow(state, config, work, calls)
    flow.adapter.poll_pull_requests = lambda _: {
        "state": "open",
        "pull_requests": prs,
    }
    advance(flow, work, calls)
    current = state.get_work(work["id"])
    assert not calls
    assert current["phase"] == "awaiting_merge"
    assert current["control"] == "backoff"
    assert len(current["data"]["pull_requests"]) == 2
    state.close()


@pytest.mark.parametrize("closure", ["pending", "not_github"])
def test_unconfirmed_source_closure_does_not_complete(tmp_path, closure):
    _, state, config = setup(tmp_path)
    work = merged_work(state, config)
    calls = []
    flow = merged_flow(state, config, work, calls, closure=closure)
    advance(flow, work, calls)
    assert calls[:2] == ["refresh", "close_issue"]
    assert calls[2][0] == "defer"
    assert state.get_work(work["id"])["phase"] == "awaiting_merge"
    state.close()


def test_missing_policy_file_after_merge_defers(tmp_path):
    _, state, config = setup(tmp_path)
    work = merged_work(state, config)
    (state.home / "prompts" / "orchestrator.md").unlink()
    calls = []
    advance(merged_flow(state, config, work, calls), work, calls)
    assert len(calls) == 1 and calls[0][0] == "defer"
    assert state.get_work(work["id"])["phase"] == "awaiting_merge"
    state.close()


def test_missing_one_required_merged_repository_defers(tmp_path):
    _, state, config = setup(tmp_path)
    config.repositories.append(
        Repository(name="cli", path=str(tmp_path / "cli"), github="owner/cli")
    )
    work = merged_work(state, config)
    work["data"]["pull_requests"].pop()
    calls = []
    advance(merged_flow(state, config, work, calls), work, calls)
    assert len(calls) == 1 and calls[0][0] == "defer"
    assert "coverage" in calls[0][2]
    assert state.get_work(work["id"])["phase"] == "awaiting_merge"
    state.close()


def test_read_only_repository_requires_evidence_but_no_pull_request(tmp_path):
    _, state, config = setup(tmp_path)
    config.repositories.append(
        Repository(name="docs", path=str(tmp_path / "docs"), access="read")
    )
    work = merged_work(state, config)
    calls = []
    advance(merged_flow(state, config, work, calls), work, calls)
    assert calls == ["refresh", "close_issue"]
    assert state.get_work(work["id"])["phase"] == "completed"
    state.close()


def test_pause_during_final_refresh_prevents_source_closure(tmp_path):
    _, state, config = setup(tmp_path)
    work = merged_work(state, config)
    calls = []
    flow = merged_flow(state, config, work, calls)
    flow.adapter.refresh_defaults = lambda _: state.pause(work["id"])
    with pytest.raises(StateConflict):
        advance(flow, work, calls)
    assert not calls
    assert state.get_work(work["id"])["control"] == "paused"
    assert state.get_work(work["id"])["phase"] == "awaiting_merge"
    state.close()


def test_configuration_change_during_refresh_prevents_source_closure(tmp_path):
    _, state, config = setup(tmp_path)
    work = merged_work(state, config)
    calls = []
    flow = merged_flow(state, config, work, calls)
    flow.adapter.refresh_defaults = lambda _: (
        state.home / "prompts" / "orchestrator.md"
    ).write_text("Policy changed during fetch")
    advance(flow, work, calls)
    assert len(calls) == 1 and calls[0][0] == "defer"
    assert "stale" in calls[0][2]
    assert state.get_work(work["id"])["phase"] == "awaiting_merge"
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
