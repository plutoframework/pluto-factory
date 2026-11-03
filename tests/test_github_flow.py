import copy
import time
from types import SimpleNamespace

import pytest

from pluto_factory.config import digest, resolved_bundle, write_config
from pluto_factory.engine import Engine
from pluto_factory.github_flow import GitHubFlow
from pluto_factory.models import Decision, FactoryConfig, Repository, Submission
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
    work = merged_work(state, config)
    flow = GitHubFlow(state, config)
    prs = [
        {"url": "https://github.com/owner/core/pull/1", "head_changed": False}
    ]
    flow.adapter = SimpleNamespace(publish=lambda _work, _entries: prs)
    flow.publish(work, work["data"]["entries"], [])
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


def advance(flow, work, calls, reassess=None, confirm=None):
    flow.advance(
        work,
        lambda *_: calls.append("revise"),
        lambda current, reason: calls.append(("defer", current, reason)),
        reassess=reassess,
        confirm=confirm,
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

    def reassess(current):
        assert state.get_work(current["id"])["updated"] == current["updated"]
        return state.update_work(
            current["id"], data=current["data"], event="acceptance.post_merge"
        )

    advance(
        merged_flow(state, config, work, calls), work, calls, reassess=reassess
    )
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


def test_ci_only_incomplete_criterion_completes_after_fresh_assessment(
    tmp_path,
):
    _, state, config = setup(tmp_path)
    work = merged_work(state, config, incomplete=True)
    work["data"]["acceptance_decision"]["acceptance"][0]["explanation"] = (
        "Wait for CI to be green"
    )
    work["data"]["pull_requests"][0]["checks"] = [
        {"name": "validate", "conclusion": "SUCCESS"}
    ]
    calls = []
    observed = []

    def reassess(current):
        persisted = state.get_work(current["id"])
        assert persisted["updated"] == current["updated"]
        assert persisted["data"]["pull_requests"][0]["state"] == "merged"
        assert current["data"]["pull_requests"][0]["checks"][0][
            "conclusion"
        ] == ("SUCCESS")
        assert (
            current["data"]["pull_requests"][0]["observed_head"]
            == (current["data"]["entries"][0]["candidate"])
        )
        observed.append(current["updated"])
        data = current["data"]
        evidence_id = state.evidence(
            current["id"],
            {
                "kind": "github_checks",
                "passed": True,
                "candidate_set": data["acceptance_candidate"],
                "config_digest": data["acceptance_config_digest"],
            },
        )
        data["acceptance_decision"]["action"] = "complete"
        data["acceptance_decision"]["acceptance"][0].update(
            result="passed",
            evidence_refs=[evidence_id],
            explanation="CI is green at the validated PR head",
        )
        return state.update_work(
            current["id"], data=data, event="acceptance.post_merge"
        )

    advance(
        merged_flow(state, config, work, calls), work, calls, reassess=reassess
    )
    current = state.get_work(work["id"])
    assert observed and observed[0] > work["updated"]
    assert calls == ["refresh", "close_issue"]
    assert current["phase"] == "completed"
    assert "increment" not in current["data"]
    assert "merged_increments" not in current["data"]
    assert current["data"]["entries"] == work["data"]["entries"]
    state.close()


def test_reassessment_waiting_returns_without_continuation(tmp_path):
    _, state, config = setup(tmp_path)
    work = merged_work(state, config, incomplete=True)
    calls = []

    def reassess(current):
        state.update_work(
            current["id"],
            control="backoff",
            wake_at=time.time() + 60,
            event="acceptance.waiting_for_remote",
        )

    advance(
        merged_flow(state, config, work, calls), work, calls, reassess=reassess
    )
    current = state.get_work(work["id"])
    assert not calls
    assert current["phase"] == "awaiting_merge"
    assert current["control"] == "backoff"
    assert current["wake_at"] > time.time()
    assert "increment" not in current["data"]
    assert current["data"]["pull_requests"][0]["state"] == "merged"
    state.close()


def test_incomplete_without_reassessment_never_blindly_queues(tmp_path):
    _, state, config = setup(tmp_path)
    work = merged_work(state, config, incomplete=True)
    calls = []
    advance(merged_flow(state, config, work, calls), work, calls)
    assert len(calls) == 1 and calls[0][0] == "defer"
    assert "fresh acceptance" in calls[0][2]
    current = state.get_work(work["id"])
    assert current["phase"] == "awaiting_merge"
    assert "increment" not in current["data"]
    assert current["data"]["pull_requests"][0]["state"] == "merged"
    state.close()


def test_stale_reassessment_result_cannot_overwrite_current_work(tmp_path):
    _, state, config = setup(tmp_path)
    work = merged_work(state, config, incomplete=True)
    calls = []
    with pytest.raises(StateConflict, match="post-merge acceptance"):
        advance(
            merged_flow(state, config, work, calls),
            work,
            calls,
            reassess=lambda current: work,
        )
    assert not calls
    assert state.get_work(work["id"])["phase"] == "awaiting_merge"
    state.close()


def test_callback_can_continue_genuine_revision_in_fresh_namespace(tmp_path):
    _, state, config = setup(tmp_path)
    work = merged_work(state, config, incomplete=True)
    data = work["data"]
    data["increment"] = 1
    data["merged_increments"] = [{"increment": 4, "pull_requests": []}]
    work = state.update_work(work["id"], data=data)
    calls = []
    flow = merged_flow(state, config, work, calls)

    def reassess(current):
        flow.continue_after_merge(
            current,
            Decision(action="revise", rationale="Fix a genuine regression"),
            ["Fix the regression identified after merge"],
        )

    advance(flow, work, calls, reassess=reassess)
    current = state.get_work(work["id"])
    assert not calls
    assert current["phase"] == "queued"
    assert current["data"]["increment"] == 5
    assert current["current_run"] is None
    assert current["data"]["merged_increments"][-1]["increment"] == 1
    assert current["data"]["merged_increments"][-1]["acceptance_decision"][
        "action"
    ] == ("revise")
    assert "pull_requests" not in current["data"]
    assert "entries" not in current["data"]
    state.close()


@pytest.mark.parametrize("incomplete", [False, True])
def test_confirmation_waiting_blocks_closure_and_continuation(
    tmp_path, incomplete
):
    _, state, config = setup(tmp_path)
    work = merged_work(state, config, incomplete=incomplete)
    calls = []

    def confirm(current):
        state.update_work(
            current["id"],
            control="backoff",
            wake_at=time.time() + 60,
            event="acceptance.remote_changed",
        )

    advance(
        merged_flow(state, config, work, calls),
        work,
        calls,
        reassess=lambda current: current,
        confirm=confirm,
    )
    current = state.get_work(work["id"])
    assert calls == ["refresh"]
    assert current["phase"] == "awaiting_merge"
    assert current["control"] == "backoff"
    assert current["wake_at"] > time.time()
    assert "increment" not in current["data"]
    assert current["data"]["pull_requests"][0]["state"] == "merged"
    state.close()


def test_stale_confirmation_result_cannot_overwrite_current_work(tmp_path):
    _, state, config = setup(tmp_path)
    work = merged_work(state, config)
    calls = []
    with pytest.raises(StateConflict, match="post-merge acceptance"):
        advance(
            merged_flow(state, config, work, calls),
            work,
            calls,
            confirm=lambda current: work,
        )
    assert calls == ["refresh"]
    assert state.get_work(work["id"])["phase"] == "awaiting_merge"
    state.close()


def test_confirmation_rechecks_returned_acceptance_binding(tmp_path):
    _, state, config = setup(tmp_path)
    work = merged_work(state, config)
    calls = []

    def confirm(current):
        current["data"]["acceptance_config_digest"] = "stale"
        return state.update_work(current["id"], data=current["data"])

    advance(
        merged_flow(state, config, work, calls),
        work,
        calls,
        confirm=confirm,
    )
    assert calls[0] == "refresh"
    assert len(calls) == 2 and calls[1][0] == "defer"
    assert "stale" in calls[1][2]
    assert state.get_work(work["id"])["phase"] == "awaiting_merge"
    state.close()


def test_current_confirmation_allows_guarded_completion(tmp_path):
    _, state, config = setup(tmp_path)
    work = merged_work(state, config)
    calls = []

    def confirm(current):
        assert state.get_work(current["id"])["updated"] == current["updated"]
        calls.append("confirm")
        return current

    advance(
        merged_flow(state, config, work, calls),
        work,
        calls,
        confirm=confirm,
    )
    assert calls == ["refresh", "confirm", "close_issue"]
    assert state.get_work(work["id"])["phase"] == "completed"
    state.close()


def test_publication_acceptance_is_frozen_before_post_merge_changes(tmp_path):
    _, state, config = setup(tmp_path)
    work = merged_work(state, config, incomplete=True)
    work["data"]["publication_acceptance"] = {"decision": "older increment"}
    work["data"]["post_merge_acceptance"] = {"fingerprint": "old"}
    work["data"]["post_merge_validation"] = {"fingerprint": "old"}
    flow = GitHubFlow(state, config)
    expected = copy.deepcopy(work["data"]["acceptance_decision"])
    flow.adapter = SimpleNamespace(
        publish=lambda item, _: item["data"]["pull_requests"]
    )
    flow.publish(work, work["data"]["entries"], ["CI not observed yet"])
    current = state.get_work(work["id"])
    assert current["data"]["publication_acceptance"]["decision"] == expected
    assert "post_merge_acceptance" not in current["data"]
    assert "post_merge_validation" not in current["data"]
    current["data"]["acceptance_decision"]["action"] = "complete"
    assert current["data"]["publication_acceptance"]["decision"]["action"] == (
        "advance"
    )
    state.close()


@pytest.mark.parametrize(
    "checkpoint", ["post_merge_acceptance", "post_merge_validation"]
)
def test_missing_frozen_publication_is_not_recaptured_after_post_merge(
    tmp_path, checkpoint
):
    _, state, config = setup(tmp_path)
    work = merged_work(state, config)
    work["data"][checkpoint] = {"fingerprint": "already-assessed"}
    calls = []
    advance(
        merged_flow(state, config, work, calls),
        work,
        calls,
        reassess=lambda current: calls.append("reassess"),
    )
    assert len(calls) == 1 and calls[0][0] == "defer"
    assert "original publication acceptance" in calls[0][2]
    assert state.get_work(work["id"])["phase"] == "awaiting_merge"
    state.close()


def test_pending_rerun_after_fresh_acceptance_can_reassess_and_complete(
    tmp_path,
):
    from pluto_factory.post_merge import confirm_merged, reassess_merged

    home, state, _ = setup(tmp_path)
    state.close()
    engine = Engine(home)
    engine.config.github_required_checks = ["validate"]
    work = merged_work(engine.state, engine.config, count=2, incomplete=True)
    remote = copy.deepcopy(work["data"]["pull_requests"])
    remote[0]["checks"] = [
        {"name": "validate", "status": "COMPLETED", "conclusion": "SUCCESS"}
    ]
    closed = []
    model_calls = []

    def decide(snapshot, allowed, prompt):
        refs = [item["id"] for item in snapshot["evidence"]]
        assert any(item["kind"] == "github_ci" for item in snapshot["evidence"])
        model_calls.append(snapshot["report"]["stage"])
        if len(model_calls) == 1:
            remote[0]["checks"][0].update(status="IN_PROGRESS", conclusion="")
        return Decision(
            action="complete",
            rationale="Fresh product and CI acceptance",
            acceptance=[
                {
                    "criterion": index,
                    "result": "passed",
                    "evidence_refs": refs,
                    "explanation": "Product and observed CI are passing",
                }
                for index in range(2)
            ],
        )

    engine.coordinator = SimpleNamespace(decide=decide)
    engine.github.adapter = SimpleNamespace(
        poll_pull_requests=lambda _: {
            "state": "merged",
            "pull_requests": copy.deepcopy(remote),
        },
        refresh_defaults=lambda _: None,
        complete_sources=lambda current: (
            closed.append(current["id"]) or {"state": "closed"}
        ),
    )

    def step(current):
        engine.github.advance(
            current,
            engine._revise,
            engine._defer,
            reassess=lambda item: reassess_merged(engine, item),
            confirm=lambda item: confirm_merged(engine, item),
        )
        return engine.state.get_work(current["id"])

    try:
        waiting = step(work)
        assert waiting["phase"] == "awaiting_merge"
        assert waiting["control"] == "backoff"
        assert waiting["data"]["acceptance_decision"]["action"] == "complete"
        assert waiting["data"]["publication_acceptance"]["decision"][
            "action"
        ] == ("advance")
        assert not closed
        remote[0]["checks"][0].update(status="COMPLETED", conclusion="SUCCESS")
        completed = step(waiting)
        assert completed["phase"] == "completed"
        assert closed == [work["id"]]
        assert model_calls == ["post_merge", "post_merge"]
        assert "increment" not in completed["data"]
        assert not engine.state.inspect()["runs"]
        assert not any(
            event["kind"] == "work.deferred"
            for event in engine.state.inspect()["events"]
        )
    finally:
        engine.state.close()


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
