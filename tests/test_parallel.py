"""Parallel workers: config, admission limits, isolation and the merge queue."""

import json
import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from pluto_factory.config import load_config, write_config
from pluto_factory.demo import create_repository
from pluto_factory.engine import Engine
from pluto_factory.github_flow import GitHubFlow
from pluto_factory.models import (
    ConcurrencyPolicy,
    FactoryConfig,
    Repository,
    Submission,
)
from pluto_factory.scheduler import (
    Scheduler,
    estimate_paths,
    paths_overlap,
)
from pluto_factory.state import State

FLAGS = [{"PLUTO_DEMO_FEATURE": "0"}, {"PLUTO_DEMO_FEATURE": "1"}]


def build(tmp_path, names, *, items=None, **settings):
    """A fixture factory with one writable repository per name."""
    repos = [create_repository(tmp_path / "sources" / name) for name in names]
    home = tmp_path / "factory"
    command = [["python3", "-m", "unittest"]]
    config = FactoryConfig(
        name="par",
        harness="fixture",
        coordinator="rules",
        isolation="cooperative",
        retry_base_seconds=0,
        repositories=[
            Repository(
                name=repo.name,
                path=str(repo),
                checks=command,
                qa=command,
                flag_environments=FLAGS,
            )
            for repo in repos
        ],
        **settings,
    )
    write_config(home, config)
    state = State(home)
    ids = []
    for index, repo in enumerate(items or names):
        target = repo if repo in names else names[0]
        ids.append(
            state.submit(
                Submission(
                    title=f"Fix {repo} {index}",
                    instruction="Fix add(left, right). The recipe edits it.",
                    acceptance=["add(2, 3) returns 5"],
                    repositories=[target],
                    source="test",
                    delivery_key=f"item-{index}",
                    fixture_scenario="fix",
                )
            )["id"]
        )
    state.close()
    return home, ids


def parallel(**overrides):
    return {
        "concurrency": ConcurrencyPolicy(
            max_workers=3, start_stagger_seconds=0, **overrides
        )
    }


# -- configuration ------------------------------------------------------


def test_defaults_preserve_serial_behaviour():
    policy = ConcurrencyPolicy()
    assert policy.max_workers == 1
    assert not policy.parallel
    assert policy.overlap == "none"
    assert policy.repository_limit("core") == 1
    assert not FactoryConfig(
        name="x", repositories=[Repository(name="a", path="/")]
    ).merge_manager_enabled


def test_parallel_defaults_and_overrides():
    policy = ConcurrencyPolicy(
        max_workers=3, repositories={"cli": 1}, max_per_repository=2
    )
    assert policy.overlap == "repository"
    assert policy.repository_limit("cli") == 1
    assert policy.repository_limit("core") == 2
    assert ConcurrencyPolicy(max_workers=3).repository_limit("core") == 3
    assert ConcurrencyPolicy(max_workers=3, avoid_overlap="none").overlap == (
        "none"
    )


@pytest.mark.parametrize(
    "bad",
    [
        {"max_workers": 0},
        {"max_workers": 9},
        {"max_per_phase": {"nonsense": 1}},
        {"max_per_phase": {"checking": 0}},
        {"repositories": {"cli": 0}},
        {"avoid_overlap": "everything"},
    ],
)
def test_invalid_concurrency_is_rejected(bad):
    with pytest.raises(ValidationError):
        ConcurrencyPolicy(**bad)


def test_concurrency_and_merge_manager_round_trip_through_toml(tmp_path):
    home, _ = build(
        tmp_path,
        ["core"],
        concurrency=ConcurrencyPolicy(
            max_workers=3,
            repositories={"core": 1},
            max_per_phase={"checking": 1, "qa": 1},
            min_free_memory_mb=2048,
        ),
        merge_manager={"model": "claude-fable-5-2", "max_merge_attempts": 3},
    )
    config = load_config(home)
    assert config.concurrency.max_workers == 3
    assert config.concurrency.repositories == {"core": 1}
    assert config.concurrency.max_per_phase == {"checking": 1, "qa": 1}
    assert config.concurrency.min_free_memory_mb == 2048
    assert config.merge_manager_enabled
    profile = config.role_profile("merge_manager")
    assert profile.model == "claude-fable-5-2"
    assert profile.provider == config.reviewer.provider  # inherited
    assert (home / "prompts" / "merge_manager.md").exists()


# -- admission ------------------------------------------------------------


def work(
    wid,
    phase="queued",
    *,
    repos=("core",),
    run=None,
    control="ready",
    text="",
    **data,
):
    return {
        "id": wid,
        "phase": phase,
        "control": control,
        "current_run": run,
        "payload": {
            "repositories": list(repos),
            "instruction": text,
            "acceptance": [],
            "title": wid,
        },
        "data": data,
    }


def scheduler(*names, now=None, **policy):
    config = FactoryConfig(
        name="x",
        repositories=[Repository(name=n, path="/") for n in names]
        or [Repository(name="core", path="/")],
        concurrency=ConcurrencyPolicy(**policy),
    )
    clock = (lambda: now[0]) if now else (lambda: 1e9)
    return Scheduler(config, clock=clock)


def test_workers_cap_new_starts():
    s = scheduler("a", "b", "c", "d", max_workers=2, start_stagger_seconds=0)
    items = [
        work("1", "implementing", repos=("a",), run="r1"),
        work("2", "checking", repos=("b",)),
        work("3", repos=("c",)),
    ]
    verdict = s.admit(items[2], items, {})
    assert not verdict.admitted and "workers" in verdict.reason
    # An item mid-pipeline is never starved by the cap.
    assert s.admit(items[1], items, {}).admitted
    # Parked items free their slot.
    items[1]["control"] = "deferred"
    assert s.admit(items[2], items, {}).admitted


def test_in_flight_start_holds_a_slot():
    s = scheduler("a", "b", max_workers=1, start_stagger_seconds=0)
    items = [work("1", repos=("a",)), work("2", repos=("b",))]
    assert not s.admit(items[1], items, {"1": "queued"}).admitted


def test_repository_limits_and_overrides():
    items = [work("1", "implementing", run="r"), work("2")]
    s = scheduler(
        "core", max_workers=3, avoid_overlap="none", start_stagger_seconds=0
    )
    assert s.admit(items[1], items, {}).admitted
    s = scheduler(
        "core",
        max_workers=3,
        repositories={"core": 1},
        avoid_overlap="none",
        start_stagger_seconds=0,
    )
    verdict = s.admit(items[1], items, {})
    assert not verdict.admitted and "core" in verdict.reason
    s = scheduler("core", max_workers=3, start_stagger_seconds=0)
    assert not s.admit(items[1], items, {}).admitted  # default overlap


def test_per_phase_throttle_counts_long_checks_in_flight():
    s = scheduler("a", "b", "c", max_workers=3, max_per_phase={"checking": 1})
    items = [
        work("1", "checking", repos=("a",)),
        work("2", "checking", repos=("b",)),
    ]
    assert s.admit(items[1], items, {}).admitted  # nothing running yet
    verdict = s.admit(items[1], items, {"1": "checking"})
    assert not verdict.admitted and "checking" in verdict.reason
    assert s.admit(items[0], items, {"1": "checking"}).admitted  # itself


def test_phase_cap_counts_active_runs():
    s = scheduler("a", "b", max_workers=3, max_per_phase={"implementing": 1})
    items = [
        work("1", "implementing", repos=("a",), run="r"),
        work("2", repos=("b",)),
    ]
    verdict = s.admit(items[1], items, {})
    assert not verdict.admitted and "implementing" in verdict.reason


def test_path_overlap_gate_uses_estimates_with_repository_fallback():
    s = scheduler(
        "core",
        max_workers=3,
        avoid_overlap="paths",
        start_stagger_seconds=0,
    )
    running = work("1", "implementing", run="r", text="Edit src/auth/login.py")
    disjoint = work("2", text="Update docs/guide.md")
    clash = work("3", text="Refactor src/auth/ helpers")
    unknown = work("4", text="Make it faster")
    items = [running, disjoint, clash, unknown]
    assert s.admit(disjoint, items, {}).admitted
    assert not s.admit(clash, items, {}).admitted
    assert not s.admit(unknown, items, {}).admitted  # unknown -> repository
    other_repo = work("5", repos=("elsewhere",))
    assert s.admit(other_repo, items, {}).admitted


def test_path_estimation_sources_and_overlap():
    item = work(
        "1",
        text="Fix `internal/rbac/policy.go` and cmd/serve/",
        entries=[{"diff_stat": " src/a.py | 3 ++-\n 1 file changed"}],
        reports={
            "implementer": {
                "artifacts": ["notes.md"],
                "findings": [{"location": "pkg/x.py:12"}],
            }
        },
    )
    found = estimate_paths(item)
    assert {
        "internal/rbac/policy.go",
        "cmd/serve",
        "src/a.py",
        "notes.md",
        "pkg/x.py",
    } <= found
    assert paths_overlap({"src/auth"}, {"src/auth/login.py"})
    assert not paths_overlap({"src/auth"}, {"src/authz/login.py"})
    assert estimate_paths(work("2", text="no paths here")) == frozenset()


def test_resource_guards_hold_new_starts():
    s = scheduler("core", max_workers=3, start_stagger_seconds=0)
    s.policy.min_free_memory_mb = 4096
    s.free_memory = lambda: 1000
    item = work("1")
    verdict = s.admit(item, [item], {})
    assert not verdict.admitted and "memory" in verdict.reason
    s.free_memory = lambda: 9000
    assert s.admit(item, [item], {}).admitted
    s.policy.max_load_average = 2.0
    s.load_average = lambda: 5.0
    assert "load" in s.admit(item, [item], {}).reason
    s.load_average = lambda: 0.5
    assert s.admit(item, [item], {}).admitted
    # Unreadable host metrics never block.
    s.free_memory = s.load_average = lambda: None
    assert s.admit(item, [item], {}).admitted


def test_start_stagger():
    now = [100.0]
    s = scheduler("a", "b", max_workers=3, start_stagger_seconds=30, now=now)
    first, second = work("1", repos=("a",)), work("2", repos=("b",))
    assert s.admit(first, [first, second], {}).admitted
    s.note_start()
    assert not s.admit(second, [first, second], {}).admitted
    now[0] += 31
    assert s.admit(second, [first, second], {}).admitted


# -- engine -----------------------------------------------------------------


def close(engine):
    engine.shutdown()
    engine.state.close()


def phases(engine, ids):
    return {i: engine.state.get_work(i)["phase"] for i in ids}


def test_max_workers_one_uses_the_serial_path(tmp_path):
    home, ids = build(tmp_path, ["a", "b"])
    engine = Engine(home)
    try:
        engine.tick()
        started = [i for i in ids if phases(engine, ids)[i] != "queued"]
        assert len(started) == 1
        assert engine._executor is None and not engine._running
    finally:
        close(engine)


def test_three_workers_start_three_items_concurrently(tmp_path):
    home, ids = build(
        tmp_path, ["a", "b", "c", "d"], **parallel(avoid_overlap="none")
    )
    engine = Engine(home)
    barrier = threading.Barrier(3, timeout=10)
    original = engine.git.prepare
    seen = set()

    def prepare(work_id, names):
        seen.add(threading.current_thread().name)
        barrier.wait()  # all three must be inside prepare at once
        return original(work_id, names)

    engine.git.prepare = prepare
    try:
        assert engine.tick()
        engine.drain()
        after = phases(engine, ids)
        assert sorted(after.values()).count("queued") == 1
        assert sum(p == "implementing" for p in after.values()) == 3
        assert len(seen) == 3
        waiting = [
            e
            for e in engine.state.inspect()["events"]
            if e["kind"] == "dispatch.deferred"
        ]
        assert len(waiting) == 1
        assert "workers" in waiting[0]["data"]["reason"]
    finally:
        close(engine)


def test_per_repository_limit_holds_same_repo_items(tmp_path):
    home, ids = build(
        tmp_path,
        ["core", "other"],
        items=["core", "core", "other"],
        **parallel(avoid_overlap="none", repositories={"core": 1}),
    )
    engine = Engine(home)
    try:
        engine.tick()
        engine.drain()
        after = phases(engine, ids)
        assert after[ids[0]] == "implementing"
        assert after[ids[1]] == "queued"  # core is at its limit
        assert after[ids[2]] == "implementing"
    finally:
        close(engine)


def test_same_repository_items_prepare_without_collisions(tmp_path):
    home, ids = build(
        tmp_path,
        ["core"],
        items=["core", "core", "core"],
        **parallel(avoid_overlap="none"),
    )
    engine = Engine(home)
    try:
        engine.tick()
        engine.drain()
        works = [engine.state.get_work(i) for i in ids]
        assert all(w["phase"] == "implementing" for w in works)
        trees = {w["data"]["entries"][0]["worktree"] for w in works}
        branches = {w["data"]["entries"][0]["branch"] for w in works}
        assert len(trees) == len(branches) == 3
        managed = Path(works[0]["data"]["entries"][0]["managed"])
        listing = subprocess.run(
            ["git", "-C", str(managed), "worktree", "list", "--porcelain"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        assert listing.count("worktree ") == 4  # clone + three
    finally:
        close(engine)


def test_parallel_items_complete_without_shared_state(tmp_path):
    home, _ids = build(tmp_path, ["a", "b", "c"], **parallel())
    engine = Engine(home)
    try:
        snapshot = engine.run(max_seconds=120)
    finally:
        close(engine)
    assert [w["phase"] for w in snapshot["work"]] == ["completed"] * 3
    runs = snapshot["runs"]
    assert len(runs) == 9
    assert all(r["state"] == "settled" for r in runs)
    for key in ("packet_path", "report_path", "assignment_path"):
        paths = [r["assignment"][key] for r in runs]
        assert len(set(paths)) == len(paths)
    sessions = {
        (r["work_id"], r["assignment"]["role"]): r["assignment"]["session_dir"]
        for r in runs
    }
    assert len(set(sessions.values())) == len(sessions)
    assert len({r["session"]["address"] for r in runs}) == 9
    worktrees = {
        e["worktree"] for w in snapshot["work"] for e in w["data"]["entries"]
    }
    assert len(worktrees) == 3
    assert not [
        e for e in snapshot["events"] if e["kind"] == "failure.observed"
    ]


def test_parallel_run_has_several_active_runs_at_once(tmp_path):
    home, ids = build(tmp_path, ["a", "b", "c"], **parallel())
    engine = Engine(home)
    # Keep launched runs alive so their overlap is observable.
    engine.harness.observe = lambda session, assignment: {"state": "working"}
    try:
        for _ in range(40):
            engine.tick()
            engine.drain()
            if len(engine.active_runs()) == 3 and all(
                engine.state.get_work(i)["current_run"] for i in ids
            ):
                break
        assert all(engine.state.get_work(i)["current_run"] for i in ids)
        assert len(engine.active_runs()) == 3
    finally:
        close(engine)


def test_stagger_spaces_out_starts(tmp_path):
    home, ids = build(
        tmp_path,
        ["a", "b"],
        concurrency=ConcurrencyPolicy(
            max_workers=3, start_stagger_seconds=3600
        ),
    )
    engine = Engine(home)
    try:
        engine.tick()
        engine.drain()
        started = [i for i, p in phases(engine, ids).items() if p != "queued"]
        assert len(started) == 1
    finally:
        close(engine)


def test_decisions_are_serialized_by_default(tmp_path):
    home, _ids = build(tmp_path, ["a", "b"], **parallel())
    engine = Engine(home)
    active, peak = [0], [0]
    guard = threading.Lock()
    original = engine.coordinator.decide

    def decide(*args, **kwargs):
        with guard:
            active[0] += 1
            peak[0] = max(peak[0], active[0])
        try:
            return original(*args, **kwargs)
        finally:
            with guard:
                active[0] -= 1

    engine.coordinator.decide = decide
    try:
        engine.run(max_seconds=120)
    finally:
        close(engine)
    assert peak[0] == 1


def test_state_connections_are_per_thread(tmp_path):
    state = State(tmp_path / "home")
    found = {}

    def read():
        found["paused"] = state.is_paused()
        found["db"] = state.db

    thread = threading.Thread(target=read)
    thread.start()
    thread.join()
    assert found["paused"] is False
    assert found["db"] is not state.db
    state.close()


# -- merge manager --------------------------------------------------------


def commit_default(managed, name, content):
    git = ["git", "-C", str(managed)]
    (Path(managed) / name).write_text(content)
    subprocess.run([*git, "add", name], check=True)
    subprocess.run(
        [
            *git,
            "-c",
            "user.name=T",
            "-c",
            "user.email=t@example.invalid",
            "commit",
            "-m",
            "Rework shared file\n\nIncoming change intent: default wins.",
        ],
        check=True,
        capture_output=True,
    )


def conflicted(tmp_path, phase="checking", **settings):
    home, ids = build(tmp_path, ["core"], **settings)
    engine = Engine(home)
    work_id = ids[0]
    work_item = engine.state.get_work(work_id)
    entries = engine.git.prepare(work_id, work_item["payload"]["repositories"])
    writable = entries[0]
    (Path(writable["worktree"]) / "shared.txt").write_text("candidate\n")
    entries = engine.git.snapshot(entries, work_id)
    commit_default(writable["managed"], "shared.txt", "default\n")
    data = {
        "entries": entries,
        "workspace_id": work_id,
        "reports": {"reviewer": {"outcome": "ready"}},
    }
    engine.state.update_work(
        work_id, phase=phase, control="deferred", data=data
    )
    engine.state.resume(work_id)
    return engine, work_id, writable


MERGE_MANAGER = {"merge_manager": {"enabled": True}}


def conflict_failure(engine, work_id):
    entries = engine.state.get_work(work_id)["data"]["entries"]
    with pytest.raises(Exception) as raised:
        engine.git.sync(entries, strict=True)
    return raised.value


def test_conflict_dispatches_merge_manager_with_both_intents(tmp_path):
    engine, work_id, _ = conflicted(tmp_path, **MERGE_MANAGER)
    launched = []
    engine._launch = launched.append
    try:
        engine.tick()
        work = engine.state.get_work(work_id)
        assert work["data"]["conflict_revision"]["resolver"] == "merge_manager"
        assert work["revisions"] == 0
        engine.tick()
        run = launched[0]
        assert run["role"] == "merge_manager"
        packet = json.loads(
            Path(run["assignment"]["packet_path"])
            .read_text()
            .split("\n\n", 1)[1]
        )
        context = packet["merge_context"]
        assert "BOTH intents" in context["rules"]
        assert context["this_item"]["title"].startswith("Fix core")
        conflict = context["conflicts"][0]
        assert conflict["conflicted_paths"] == ["shared.txt"]
        assert any(
            line.startswith("<<<<<<<")
            for line in conflict["hunks"]["shared.txt"][0].splitlines()
        )
        assert "candidate" in conflict["this_item_diff"]
        merged = conflict["merged_changes"][0]
        assert merged["title"] == "Rework shared file"
        assert "default wins" in merged["body"]
        assert "default" in merged["patch"]
        assert (
            "merge conflicts"
            in Path(run["assignment"]["role_path"]).read_text().lower()
        )
    finally:
        close(engine)


def test_disabled_merge_manager_keeps_the_implementer(tmp_path):
    engine, _work_id, _ = conflicted(tmp_path)
    launched = []
    engine._launch = launched.append
    try:
        engine.tick()
        engine.tick()
        assert launched[0]["role"] == "implementer"
        packet = launched[0]["assignment"]["packet_path"]
        assert "merge_context" not in Path(packet).read_text()
    finally:
        close(engine)


def test_resolution_goes_through_review_with_a_checklist(tmp_path):
    engine, work_id, writable = conflicted(tmp_path, **MERGE_MANAGER)
    roles = []
    try:
        for _ in range(60):
            engine.tick()
            engine.state.get_work(work_id)
            roles = [
                r["role"]
                for r in engine.state.inspect()["runs"]
                if r["work_id"] == work_id
            ]
            if "reviewer" in roles:
                break
        assert roles[0] == "merge_manager"
        assert "implementer" not in roles
        assert "reviewer" in roles
        reviewer = next(
            r for r in engine.state.inspect()["runs"] if r["role"] == "reviewer"
        )
        packet = json.loads(
            Path(reviewer["assignment"]["packet_path"])
            .read_text()
            .split("\n\n", 1)[1]
        )
        checklist = packet["conflict_resolution_checklist"]
        assert "marker" in checklist and "test" in checklist
        resolved = (Path(writable["worktree"]) / "shared.txt").read_text()
        assert "<<<<<<<" not in resolved
        assert "candidate" in resolved and "default" in resolved
        evidence = [
            e["data"]["kind"] for e in engine.state.inspect()["evidence"]
        ]
        assert "checks" in evidence  # checks ran before review
    finally:
        close(engine)


def test_merge_budget_exhaustion_asks_the_owner(tmp_path):
    engine, work_id, _ = conflicted(
        tmp_path,
        merge_manager={"enabled": True, "max_merge_attempts": 2},
    )
    try:
        error = conflict_failure(engine, work_id)
        for expected in (1, 2):
            work = engine.state.get_work(work_id)
            assert engine._conflict_revision(work, error)
            work = engine.state.get_work(work_id)
            assert work["phase"] == "implementing"
            assert work["data"]["merge_attempts"] == {"unpublished": expected}
            engine.state.update_work(work_id, phase="checking")
        work = engine.state.get_work(work_id)
        assert engine._conflict_revision(work, error)
        work = engine.state.get_work(work_id)
        assert work["control"] == "deferred"
        assert work["data"]["merge_attempts"] == {"unpublished": 2}
        events = [e["kind"] for e in engine.state.inspect()["events"]]
        assert "merge_manager.exhausted" in events
        decisions = engine.state.db.execute(
            "SELECT state,advice FROM decisions WHERE work_id=?", (work_id,)
        ).fetchall()
        assert len(decisions) == 1
        assert decisions[0]["advice"] == "pending"  # the advisor briefs it
    finally:
        close(engine)


def test_merge_attempts_are_budgeted_per_pr_head(tmp_path):
    engine, work_id, _ = conflicted(
        tmp_path, merge_manager={"enabled": True, "max_merge_attempts": 1}
    )
    try:
        error = conflict_failure(engine, work_id)
        work = engine.state.get_work(work_id)
        data = work["data"]
        data["merge_attempts"] = {"old-head": 1}
        data["pull_requests"] = [{"head": "new-head", "state": "open"}]
        engine.state.update_work(work_id, data=data)
        work = engine.state.get_work(work_id)
        assert engine._conflict_revision(work, error)
        work = engine.state.get_work(work_id)
        assert work["control"] != "deferred"
        assert work["data"]["merge_attempts"]["new-head"] == 1
    finally:
        close(engine)


# -- merge queue ---------------------------------------------------------


def queue_setup(tmp_path, **settings):
    config = FactoryConfig(
        name="queue",
        harness="fixture",
        coordinator="rules",
        isolation="cooperative",
        integration="github",
        github_auto_merge=settings.pop("auto", True),
        github_poll_seconds=1,
        repositories=[
            Repository(name="core", path=str(tmp_path), github="owner/core")
        ],
        **settings,
    )
    home = tmp_path / "factory"
    write_config(home, config)
    state = State(home)
    works = []
    for index in range(2):
        item = state.submit(
            Submission(
                title=f"Issue {index}",
                instruction="Do it",
                acceptance=["ok"],
                repositories=["core"],
                source="test",
                delivery_key=str(index),
            )
        )
        pr = {
            "repository": "core",
            "slug": "owner/core",
            "number": index + 1,
            "state": "open",
            "head": f"{index}" * 40,
            "merge_state": "CLEAN",
            "url": f"https://github.com/owner/core/pull/{index + 1}",
        }
        data = {"pull_requests": [pr]}
        with state.db:
            state.db.execute(
                "UPDATE work SET phase='awaiting_merge',control='backoff',"
                "data=? WHERE id=?",
                (json.dumps(data), item["id"]),
            )
        works.append(item["id"])
    return state, config, works


def advance(flow, state, work_id, pr_state="CLEAN"):
    resyncs = []
    work = state.get_work(work_id)
    pr = {**work["data"]["pull_requests"][0], "merge_state": pr_state}
    flow.adapter = SimpleNamespace(
        poll_pull_requests=lambda _prs: {
            "state": "open",
            "pull_requests": [pr],
        },
        request_merge=lambda prs: flow.requested.append(work_id),
    )
    flow.advance(
        work,
        lambda *_: None,
        lambda *_: None,
        resync=lambda item, stale: resyncs.append(item["id"]),
    )
    return resyncs


def test_merge_queue_serializes_same_repo_merges(tmp_path):
    state, config, (first, second) = queue_setup(
        tmp_path, concurrency=ConcurrencyPolicy(max_workers=3)
    )
    flow = GitHubFlow(state, config)
    flow.requested = []
    advance(flow, state, second)
    assert flow.requested == []  # waits behind the first PR
    assert any(
        e["kind"] == "merge_queue.waiting" for e in state.inspect()["events"]
    )
    advance(flow, state, first)
    assert flow.requested == [first]
    # The head merged: the next PR is now first in line.
    with state.db:
        state.db.execute(
            "UPDATE work SET phase='completed' WHERE id=?", (first,)
        )
    advance(flow, state, second)
    assert flow.requested == [first, second]
    state.close()


def test_merge_queue_resyncs_only_the_head_on_the_new_base(tmp_path):
    state, config, (first, second) = queue_setup(
        tmp_path, concurrency=ConcurrencyPolicy(max_workers=3)
    )
    flow = GitHubFlow(state, config)
    flow.requested = []
    assert advance(flow, state, second, "BEHIND") == []  # follower waits
    assert advance(flow, state, first, "BEHIND") == [first]
    with state.db:
        state.db.execute(
            "UPDATE work SET phase='completed' WHERE id=?", (first,)
        )
    assert advance(flow, state, second, "BEHIND") == [second]
    state.close()


def test_merge_queue_offers_the_head_to_the_owner_without_auto_merge(
    tmp_path,
):
    state, config, (first, second) = queue_setup(
        tmp_path, auto=False, concurrency=ConcurrencyPolicy(max_workers=3)
    )
    flow = GitHubFlow(state, config)
    flow.requested = []
    advance(flow, state, second)
    advance(flow, state, first)
    advance(flow, state, first)  # idempotent per PR head
    messages = [n["message"] for n in state.inspect()["notifications"]]
    assert len([m for m in messages if "merge queue" in m]) == 1
    assert "pull/1" in messages[0]
    assert flow.requested == []
    state.close()


def test_serial_factories_have_no_merge_queue(tmp_path):
    state, config, (first, second) = queue_setup(tmp_path)
    flow = GitHubFlow(state, config)
    flow.requested = []
    advance(flow, state, second)
    advance(flow, state, first)
    assert flow.requested == [second, first]  # unchanged behaviour
    # BEHIND only resyncs when the repo requires up-to-date branches.
    assert advance(flow, state, second, "BEHIND") == []
    state.close()
