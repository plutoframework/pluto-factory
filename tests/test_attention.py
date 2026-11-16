"""PR tracking, self-healing repair, decisions and delivery gating."""

import json
import subprocess
import time
import tomllib

import pytest

from pluto_factory import attention
from pluto_factory.attention import (
    AttentionError,
    DecisionConflict,
    TrackingRequired,
    add_decision,
    answer_decision,
    get_pr,
    list_decisions,
    list_prs,
    set_pr,
    track_pr,
)
from pluto_factory.cli import main
from pluto_factory.config import write_config
from pluto_factory.engine import Engine
from pluto_factory.models import (
    FactoryConfig,
    Repository,
    Submission,
    TrackedRepository,
)
from pluto_factory.pr_tracker import (
    classify,
    parse_repair_source,
    repair_source,
)

SHA = "a" * 40
SHA2 = "b" * 40
PASS = [{"name": "ci", "status": "COMPLETED", "conclusion": "SUCCESS"}]
FAIL = [
    {
        "name": "ci",
        "status": "COMPLETED",
        "conclusion": "FAILURE",
        "detailsUrl": "https://github.com/plutoframework/cli/actions/runs/99/job/1",
    }
]


class Gh:
    """Scripted ``gh``: PR views, PR lists and failed-run logs."""

    def __init__(self):
        self.views = {}
        self.lists = {}
        self.calls = []
        self.logs = "FAILED test_x: assert 1 == 2"

    def __call__(self, argv, **_):
        self.calls.append(list(argv))
        output = ""
        if argv[:3] == ["gh", "pr", "view"]:
            slug = argv[argv.index("--repo") + 1]
            output = json.dumps(self.views[(slug, int(argv[3]))])
        elif argv[:3] == ["gh", "pr", "list"]:
            slug = argv[argv.index("--repo") + 1]
            output = json.dumps(self.lists.get(slug, []))
        elif argv[:3] == ["gh", "run", "view"]:
            output = self.logs
        return subprocess.CompletedProcess(argv, 0, output, "")


def view(head=SHA, checks=PASS, **values):
    return {
        "number": 5,
        "title": "Add thing",
        "url": "https://github.com/plutoframework/cli/pull/5",
        "state": "OPEN",
        "headRefOid": head,
        "headRefName": "feature",
        "baseRefName": "main",
        "reviewDecision": "",
        "statusCheckRollup": checks,
        "mergeStateStatus": "CLEAN",
        "mergedAt": None,
        "isDraft": False,
        **values,
    }


def build(tmp_path, **settings):
    checkout = tmp_path / "checkout"
    checkout.mkdir(exist_ok=True)
    home = tmp_path / "factory"
    config = FactoryConfig(
        name="test",
        harness="fixture",
        coordinator="rules",
        isolation="cooperative",
        repositories=[
            Repository(
                name="core",
                path=str(checkout),
                github="owner/core",
                checks=[["true"]],
                qa=[["true"]],
            )
        ],
        tracked_repositories=[
            TrackedRepository(
                repo="plutoframework/cli",
                local_path=str(checkout),
                checks=[["true"]],
            )
        ],
        **settings,
    )
    write_config(home, config)
    engine = Engine(home)
    gh = Gh()
    engine.tracker.adapter.runner = gh
    return engine, gh


def tracked(engine, gh, number=5, **values):
    gh.views[("plutoframework/cli", number)] = values.pop("view", view())
    return track_pr(
        engine.state,
        "plutoframework/cli",
        number,
        origin="tracked",
        branch="feature",
        base="main",
        head=SHA,
        repo_name="t-plutoframework-cli",
        **values,
    )


def observe(engine, number=5):
    row = get_pr(engine.state, "plutoframework/cli", number)
    return engine.tracker.observe(row)


def states(engine):
    return [
        e["data"]["to"]
        for e in engine.state.inspect()["events"]
        if e["kind"] == "pr.state_changed"
    ]


def repairs(engine):
    return [
        w
        for w in engine.state.list_work()
        if parse_repair_source(w["payload"]["source"])
    ]


# --- PR tracker state machine ------------------------------------------------


def test_state_machine_open_to_merged(tmp_path):
    engine, gh = build(tmp_path)
    row = tracked(engine, gh)
    assert row["state"] == "open"
    gh.views[("plutoframework/cli", 5)] = view(
        checks=[{"name": "ci", "status": "IN_PROGRESS", "conclusion": ""}]
    )
    assert observe(engine)["state"] == "pending"
    gh.views[("plutoframework/cli", 5)] = view(checks=FAIL)
    assert observe(engine)["state"] == "repairing"
    assert len(repairs(engine)) == 1
    gh.views[("plutoframework/cli", 5)] = view(
        head=SHA2, checks=PASS, mergeStateStatus="BEHIND"
    )
    assert observe(engine)["state"] == "green"
    gh.views[("plutoframework/cli", 5)] = view(head=SHA2, checks=PASS)
    ready = observe(engine)
    assert ready["state"] == "ready"
    gh.views[("plutoframework/cli", 5)] = view(
        head=SHA2, state="MERGED", mergedAt="2026-01-01T00:00:00Z"
    )
    assert observe(engine)["state"] == "merged"
    assert states(engine) == [
        "pending",
        "failed",
        "repairing",
        "pending",
        "green",
        "ready",
        "merged",
    ]


def test_ready_requires_passing_checks_on_current_head(tmp_path):
    engine, gh = build(tmp_path)
    tracked(engine, gh)
    gh.views[("plutoframework/cli", 5)] = view(checks=[])
    assert observe(engine)["state"] == "pending"
    row = get_pr(engine.state, "plutoframework/cli", 5)
    old = classify(view(checks=[]), {**row, "state_since": 0}, engine.config)
    assert old["state"] == "ready"  # no CI at all, after the grace period
    gh.views[("plutoframework/cli", 5)] = view(checks=FAIL)
    assert observe(engine)["state"] != "ready"


def test_review_and_branch_protection_need_the_owner(tmp_path):
    engine, gh = build(tmp_path)
    tracked(engine, gh)
    gh.views[("plutoframework/cli", 5)] = view(reviewDecision="REVIEW_REQUIRED")
    row = observe(engine)
    assert row["state"] == "needs_owner" and "review" in row["reason"]
    gh.views[("plutoframework/cli", 5)] = view(mergeStateStatus="BLOCKED")
    assert observe(engine)["state"] == "needs_owner"


def test_codeql_failure_is_not_code_fixable(tmp_path):
    engine, gh = build(tmp_path)
    tracked(engine, gh)
    gh.views[("plutoframework/cli", 5)] = view(
        checks=[
            {"name": "CodeQL", "status": "COMPLETED", "conclusion": "FAILURE"}
        ]
    )
    row = observe(engine)
    assert row["state"] == "needs_owner"
    assert "code-scanning" in row["reason"]
    assert not repairs(engine)


def test_missing_secret_log_is_not_code_fixable(tmp_path):
    engine, gh = build(tmp_path)
    tracked(engine, gh)
    gh.logs = "Error: secret NPM_TOKEN is not set"
    gh.views[("plutoframework/cli", 5)] = view(checks=FAIL)
    row = observe(engine)
    assert row["state"] == "needs_owner"
    assert not repairs(engine)


def test_failure_records_check_names_and_bounded_log(tmp_path):
    engine, gh = build(tmp_path)
    tracked(engine, gh)
    gh.logs = "\x1b[31m" + "x" * 10000
    gh.views[("plutoframework/cli", 5)] = view(checks=FAIL)
    row = observe(engine)
    assert row["failing"] == ["ci"]
    assert 0 < len(row["log_excerpt"]) <= 3000
    assert "\x1b" not in row["log_excerpt"]


def test_budget_exhaustion_escalates_with_a_decision(tmp_path):
    engine, gh = build(tmp_path, pr_max_repairs=1)
    tracked(engine, gh)
    gh.views[("plutoframework/cli", 5)] = view(checks=FAIL)
    assert observe(engine)["state"] == "repairing"
    (work,) = repairs(engine)
    # The repair ends without changing the head: budget for SHA is spent.
    engine.state.update_work(work["id"], phase="completed", control="settled")
    row = observe(engine)
    assert row["state"] == "needs_owner"
    assert "budget exhausted" in row["reason"]
    (decision,) = list_decisions(engine.state, {"open"})
    assert decision["kind"] == "choice"
    assert any(o["recommended"] for o in decision["options"])
    assert len(repairs(engine)) == 1  # no second repair for that head
    # Owner grants another attempt; the coordinator resets the budget.
    answer_decision(
        engine.state, decision["id"], "Grant another repair attempt"
    )
    engine.process_decisions()
    assert get_pr(engine.state, "plutoframework/cli", 5)["state"] == "failed"
    assert observe(engine)["state"] == "repairing"
    assert len(repairs(engine)) == 2


def test_exactly_one_repair_per_failed_head_and_respects_pause(tmp_path):
    engine, gh = build(tmp_path)
    tracked(engine, gh)
    gh.views[("plutoframework/cli", 5)] = view(checks=FAIL)
    engine.state.pause()
    observe(engine)
    assert not repairs(engine)
    engine.state.resume()
    observe(engine)
    observe(engine)
    observe(engine)
    assert len(repairs(engine)) == 1
    work = repairs(engine)[0]
    assert work["payload"]["source"] == repair_source("plutoframework/cli", 5)
    assert work["payload"]["delivery_key"] == f"{SHA}:1"


def test_stacked_pr_is_not_repaired_until_base_lands(tmp_path):
    engine, gh = build(tmp_path)
    track_pr(
        engine.state,
        "plutoframework/cli",
        4,
        origin="tracked",
        branch="base-branch",
        base="main",
        head=SHA2,
    )
    tracked(engine, gh)
    gh.views[("plutoframework/cli", 5)] = view(
        checks=FAIL, baseRefName="base-branch"
    )
    row = observe(engine)
    assert row["state"] == "failed" and "Stacked" in row["reason"]
    assert not repairs(engine)


def test_repair_packet_carries_failure_and_no_weakening_rule(tmp_path):
    engine, gh = build(tmp_path)
    tracked(engine, gh)
    gh.views[("plutoframework/cli", 5)] = view(checks=FAIL)
    observe(engine)
    (work,) = repairs(engine)
    packet = engine._repair_packet(work)
    assert packet["failing_checks"] == ["ci"]
    assert "FAILED test_x" in packet["log_excerpt"]
    assert "Do NOT weaken" in packet["rules"]
    assert work["payload"]["repositories"] == ["t-plutoframework-cli"]


def test_unconfigured_checkout_escalates_instead_of_repairing(tmp_path):
    engine, gh = build(tmp_path)
    engine.config.tracked_repositories[0].local_path = ""
    tracked(engine, gh)
    gh.views[("plutoframework/cli", 5)] = view(checks=FAIL)
    row = observe(engine)
    assert row["state"] == "needs_owner"
    assert "no local checkout" in row["reason"]


def test_tracked_repository_open_prs_are_discovered(tmp_path):
    engine, gh = build(tmp_path)
    gh.lists["plutoframework/cli"] = [
        {
            "number": 7,
            "title": "Lead PR",
            "url": "https://github.com/plutoframework/cli/pull/7",
            "headRefName": "lead",
            "baseRefName": "main",
            "headRefOid": SHA,
        }
    ]
    gh.views[("plutoframework/cli", 7)] = view(number=7)
    engine.tracker.poll(force=True)
    row = get_pr(engine.state, "plutoframework/cli", 7)
    assert row["origin"] == "tracked" and row["state"] == "ready"


def test_prs_cli_track_list_untrack(tmp_path, capsys):
    engine, _ = build(tmp_path)
    home = str(engine.home)
    assert main(["prs", "track", "plutoframework/cli#9", "--home", home]) == 0
    capsys.readouterr()
    main(["prs", "list", "--home", home])
    listed = json.loads(capsys.readouterr().out)["pull_requests"]
    assert [pr["number"] for pr in listed] == [9]
    main(["prs", "untrack", "plutoframework/cli#9", "--home", home])
    capsys.readouterr()
    main(["prs", "list", "--home", home])
    assert json.loads(capsys.readouterr().out)["pull_requests"] == []
    assert main(["prs", "track", "bad", "--home", home]) == 1


# --- publication path, adoption and the delivery gate -----------------------


def test_untracked_factory_branch_pr_is_adopted_and_flagged(tmp_path):
    engine, gh = build(tmp_path)
    gh.lists["owner/core"] = [
        {
            "number": 3,
            "title": "Factory work",
            "url": "https://github.com/owner/core/pull/3",
            "headRefName": "factory/work-abc",
            "baseRefName": "main",
            "headRefOid": SHA,
        },
        {
            "number": 4,
            "title": "Someone else",
            "url": "https://github.com/owner/core/pull/4",
            "headRefName": "feature/unrelated",
            "baseRefName": "main",
            "headRefOid": SHA,
        },
    ]
    engine.tracker.sweep()
    assert get_pr(engine.state, "owner/core", 3)["origin"] == "factory"
    assert get_pr(engine.state, "owner/core", 4) is None
    kinds = [e["kind"] for e in engine.state.inspect()["events"]]
    assert "pr.adopted_untracked" in kinds
    assert any(
        "Adopted" in n["message"]
        for n in engine.state.inspect()["notifications"]
    )


def test_crash_between_intent_and_number_is_reconciled(tmp_path):
    engine, gh = build(tmp_path)
    intent = attention.record_intent(
        engine.state, "owner/core", "factory/work-x", "main", SHA, None, "core"
    )
    assert attention.pending_intents(engine.state)
    gh.lists["owner/core"] = [
        {
            "number": 8,
            "title": "t",
            "url": "https://github.com/owner/core/pull/8",
            "headRefName": "factory/work-x",
            "baseRefName": "main",
            "headRefOid": SHA,
        }
    ]
    engine.tracker.sweep()
    row = get_pr(engine.state, "owner/core", 8)
    assert row["origin"] == "factory"
    assert not attention.pending_intents(engine.state)
    del intent


def test_delivery_cannot_advance_without_a_tracker_row(tmp_path):
    engine, _ = build(tmp_path)
    work = engine.state.submit(
        Submission(
            title="w",
            instruction="i",
            acceptance=["a"],
            repositories=["core"],
        )
    )
    prs = [{"slug": "owner/core", "number": 11, "url": "u"}]
    for phase in ("awaiting_merge", "completed"):
        with pytest.raises(TrackingRequired):
            engine.state.update_work(
                work["id"], phase=phase, data={"pull_requests": prs}
            )
        assert engine.state.get_work(work["id"])["phase"] == "queued"
    track_pr(engine.state, "owner/core", 11, origin="factory")
    done = engine.state.update_work(
        work["id"], phase="awaiting_merge", data={"pull_requests": prs}
    )
    assert done["phase"] == "awaiting_merge"


def test_github_integration_requires_prs_to_await_merge(tmp_path):
    engine, _ = build(tmp_path, integration="github")
    work = engine.state.submit(
        Submission(
            title="w", instruction="i", acceptance=["a"], repositories=["core"]
        )
    )
    with pytest.raises(TrackingRequired):
        engine.state.update_work(work["id"], phase="awaiting_merge")


def test_publish_records_intent_before_create_and_row_after(tmp_path):
    from test_github import NEW, adapter, entries, work

    integration, remote = adapter(tmp_path)
    order = []
    original = remote.__call__

    def spy(argv, **kwargs):
        if argv[:3] == ["gh", "pr", "create"]:
            rows = attention.list_prs(integration.ledger)
            order.append(("create", [(r["number"], r["head"]) for r in rows]))
        return original(argv, **kwargs)

    integration.runner = spy
    published = integration.publish(work(), entries(integration))
    assert order == [("create", [(None, NEW)])]
    row = get_pr(integration.ledger, "owner/core", published[0]["number"])
    assert row["url"] == published[0]["url"] and row["origin"] == "factory"


def test_gh_pr_create_is_refused_outside_the_publication_path(tmp_path):
    from test_github import adapter

    integration, remote = adapter(tmp_path)
    from pluto_factory.github import GitHubFailure

    with pytest.raises(GitHubFailure) as failure:
        integration._run(["gh", "pr", "create"], mutating=True)
    assert failure.value.code == "tracking.required"
    assert not remote.calls


# --- workers cannot publish -------------------------------------------------


def test_worker_environment_has_no_github_credentials(tmp_path):
    from pluto_factory.worker_launcher import worker_environment

    env = worker_environment(
        {
            "GH_TOKEN": "x",
            "GITHUB_TOKEN": "y",
            "SSH_AUTH_SOCK": "/s",
            "PATH": "/bin",
        },
        str(tmp_path),
    )
    assert "GH_TOKEN" not in env and "GITHUB_TOKEN" not in env
    assert "SSH_AUTH_SOCK" not in env and env["PATH"] == "/bin"
    assert env["GH_CONFIG_DIR"] == str(tmp_path)
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert env["GIT_CONFIG_GLOBAL"] == "/dev/null"


def test_sandbox_rejects_github_tokens_in_passthrough():
    from pluto_factory.models import SandboxPolicy

    with pytest.raises(ValueError):
        SandboxPolicy(env_passthrough=["GH_TOKEN"])
    with pytest.raises(ValueError):
        SandboxPolicy(env_passthrough=["GITHUB_TOKEN"])


# --- decisions ---------------------------------------------------------------


def make_work(engine, title="Build it"):
    return engine.state.submit(
        Submission(
            title=title,
            instruction="i",
            acceptance=["a"],
            repositories=["core"],
        )
    )


def test_decision_create_dedupe_and_options(tmp_path):
    engine, _ = build(tmp_path)
    work = make_work(engine)
    options = [
        {"label": "A", "recommended": True},
        {"label": "B", "description": "other"},
    ]
    first = add_decision(
        engine.state,
        question="Pick?",
        options=options,
        kind="choice",
        work_id=work["id"],
    )
    again = add_decision(
        engine.state,
        question="Pick?",
        options=options,
        kind="choice",
        work_id=work["id"],
    )
    assert first["id"] == again["id"]
    assert len(list_decisions(engine.state)) == 1
    assert first["state"] == "open" and first["options"][0]["recommended"]
    other = add_decision(
        engine.state, question="Pick?", kind="free_text", work_id=None
    )
    assert other["id"] != first["id"]
    notices = [n["kind"] for n in engine.state.inspect()["notifications"]]
    assert notices.count("decision") == 2
    with pytest.raises(AttentionError):
        add_decision(
            engine.state, question="x", kind="choice", options=["only"]
        )
    with pytest.raises(AttentionError):
        answer_decision(engine.state, first["id"], "not an option")


def test_decision_answered_via_cli_resumes_work_with_guidance(tmp_path, capsys):
    engine, _ = build(tmp_path)
    work = make_work(engine)
    engine._defer(engine.state.get_work(work["id"]), "needs a call")
    (decision,) = list_decisions(engine.state, {"open"})
    assert decision["work_id"] == work["id"]
    assert engine.state.get_work(work["id"])["control"] == "deferred"
    home = str(engine.home)
    code = main(
        [
            "decisions",
            "answer",
            decision["id"],
            "--answer",
            "Retry as planned",
            "--note",
            "use sqlite",
            "--home",
            home,
        ]
    )
    assert code == 0
    capsys.readouterr()
    assert list_decisions(engine.state, {"answered"})
    # Answering alone does nothing to the work; the coordinator applies it.
    assert engine.state.get_work(work["id"])["control"] == "deferred"
    engine.process_decisions()
    resumed = engine.state.get_work(work["id"])
    assert resumed["control"] == "ready"
    (guidance,) = engine.state.guidance(work["id"])
    assert guidance["text"].startswith(
        f"Owner decision on '{decision['question']}': Retry as planned"
    )
    assert "use sqlite" in guidance["text"]
    assert (
        attention.get_decision(engine.state, decision["id"])["state"]
        == "applied"
    )
    with pytest.raises(DecisionConflict):
        answer_decision(engine.state, decision["id"], "again")


def test_decisions_cli_add_list_show(tmp_path, capsys):
    engine, _ = build(tmp_path)
    work = make_work(engine)
    home = str(engine.home)
    code = main(
        [
            "decisions",
            "add",
            "--question",
            "Which db?",
            "--work",
            work["id"],
            "--option",
            "sqlite=simple",
            "--option",
            "postgres",
            "--recommended",
            "sqlite",
            "--home",
            home,
        ]
    )
    assert code == 0
    added = json.loads(capsys.readouterr().out)
    assert added["kind"] == "choice"
    assert [o["recommended"] for o in added["options"]] == [True, False]
    main(["decisions", "list", "--home", home])
    assert len(json.loads(capsys.readouterr().out)["decisions"]) == 1
    main(["decisions", "show", added["id"], "--home", home])
    assert json.loads(capsys.readouterr().out)["id"] == added["id"]
    assert main(["decisions", "add", "--home", home]) == 1


def test_config_decision_applies_validated_change_with_backup(tmp_path):
    engine, _ = build(tmp_path)
    work = make_work(engine)
    toml = engine.home / "factory.toml"
    engine.state.update_work(work["id"], control="deferred")
    decision = add_decision(
        engine.state,
        question="Raise the phase timeout?",
        kind="config",
        work_id=work["id"],
        proposal={"key": "phase_timeout_seconds", "value": 3600},
    )
    answer_decision(engine.state, decision["id"], "Apply")
    engine.process_decisions()
    assert tomllib.loads(toml.read_text())["phase_timeout_seconds"] == 3600
    assert engine.config.phase_timeout_seconds == 3600
    assert list(engine.home.glob("factory.toml.bak-*"))
    assert engine.state.get_work(work["id"])["control"] == "ready"
    assert (
        "factory.toml updated" in engine.state.guidance(work["id"])[0]["text"]
    )
    assert (
        attention.get_decision(engine.state, decision["id"])["state"]
        == "applied"
    )


def test_config_decision_declined_changes_nothing(tmp_path):
    engine, _ = build(tmp_path)
    before = (engine.home / "factory.toml").read_text()
    decision = add_decision(
        engine.state,
        question="Raise?",
        kind="config",
        proposal={"key": "phase_timeout_seconds", "value": 3600},
    )
    answer_decision(engine.state, decision["id"], "Decline")
    engine.process_decisions()
    assert (engine.home / "factory.toml").read_text() == before
    assert not list(engine.home.glob("factory.toml.bak-*"))


def test_config_decision_rejects_unsafe_keys_and_invalid_values(tmp_path):
    engine, _ = build(tmp_path)
    for proposal in (
        {"key": "dangerously_skip_permissions", "value": 1},
        {"key": "phase_timeout_seconds", "value": "soon"},
        {"key": "phase_timeout_seconds", "value": -5},
    ):
        with pytest.raises(AttentionError):
            add_decision(
                engine.state, question="x", kind="config", proposal=proposal
            )
    # Validation failure at apply time leaves the live file untouched.
    before = (engine.home / "factory.toml").read_text()
    with pytest.raises(ValueError):
        attention.apply_config_change(engine.home, "phase_timeout_seconds", 0)
    assert (engine.home / "factory.toml").read_text() == before


def test_config_change_replaces_existing_and_inserts_new_keys(tmp_path):
    engine, _ = build(tmp_path)
    attention.apply_config_change(engine.home, "max_revisions", 9)
    attention.apply_config_change(engine.home, "max_revisions", 10)
    parsed = tomllib.loads((engine.home / "factory.toml").read_text())
    assert parsed["max_revisions"] == 10
    attention.apply_config_change(
        engine.home, "implementer_phase_timeout_seconds", 7200
    )
    parsed = tomllib.loads((engine.home / "factory.toml").read_text())
    assert parsed["implementer_phase_timeout_seconds"] == 7200
    assert parsed["repositories"][0]["name"] == "core"


def test_old_ledger_without_attention_tables_still_reads(tmp_path):
    import sqlite3

    connection = sqlite3.connect(":memory:")
    assert attention.read_attention(connection) == {
        "decisions": [],
        "pull_requests": [],
    }


def test_set_pr_notifies_once_per_head_for_owner_states(tmp_path):
    engine, gh = build(tmp_path)
    row = tracked(engine, gh)
    set_pr(engine.state, row, state="ready", reason="ok")
    row = get_pr(engine.state, "plutoframework/cli", 5)
    set_pr(engine.state, row, state="pending")
    row = get_pr(engine.state, "plutoframework/cli", 5)
    set_pr(engine.state, row, state="ready", reason="ok")
    notes = [
        n
        for n in engine.state.inspect()["notifications"]
        if n["kind"] == "attention"
    ]
    assert len(notes) == 1
    assert list_prs(engine.state, {"ready"})
    assert time.time() - notes[0]["created"] < 60


# --- repair push -------------------------------------------------------------


def repair_work(engine, gh):
    tracked(engine, gh)
    gh.views[("plutoframework/cli", 5)] = view(checks=FAIL)
    observe(engine)
    (work,) = repairs(engine)
    return work


def test_repair_push_updates_tracker_and_completes(tmp_path):
    engine, gh = build(tmp_path)
    work = repair_work(engine, gh)
    pushed = []
    engine.tracker.adapter.push_repair = lambda entry, slug, branch, head: (
        pushed.append((slug, branch, head)) or entry["candidate"]
    )
    data = {
        "entries": [{"candidate": SHA2, "pr_branch": "feature"}],
        "repair": {"slug": "plutoframework/cli", "number": 5, "head": SHA},
    }
    item = engine.state.update_work(work["id"], phase="pushing", data=data)
    engine._push_repair(item, ("plutoframework/cli", 5))
    assert pushed == [("plutoframework/cli", "feature", SHA)]
    row = get_pr(engine.state, "plutoframework/cli", 5)
    assert row["state"] == "pending" and row["head"] == SHA2
    assert engine.state.get_work(work["id"])["phase"] == "completed"


def test_repair_without_changes_escalates_to_the_owner(tmp_path):
    engine, gh = build(tmp_path)
    work = repair_work(engine, gh)
    data = {
        "entries": [{"candidate": SHA, "pr_branch": "feature"}],
        "repair": {"slug": "plutoframework/cli", "number": 5, "head": SHA},
    }
    item = engine.state.update_work(work["id"], phase="pushing", data=data)
    engine._push_repair(item, ("plutoframework/cli", 5))
    row = get_pr(engine.state, "plutoframework/cli", 5)
    assert row["state"] == "needs_owner"
    assert engine.state.get_work(work["id"])["control"] == "deferred"
    assert list_decisions(engine.state, {"open"})


def test_deferred_repair_surfaces_as_needs_owner_on_next_poll(tmp_path):
    engine, gh = build(tmp_path)
    work = repair_work(engine, gh)
    engine._defer(engine.state.get_work(work["id"]), "cannot fix")
    row = observe(engine)
    assert row["state"] == "needs_owner"


# --- repair review -----------------------------------------------------------


def test_repair_goes_through_review_before_push(tmp_path):
    engine, gh = build(tmp_path)
    work = repair_work(engine, gh)
    data = {
        "entries": [{"name": "r", "candidate": SHA2, "pr_branch": "feature"}],
        "repair": {"slug": "plutoframework/cli", "number": 5, "head": SHA},
    }
    engine.state.update_work(work["id"], phase="checking", data=data)
    engine.git.snapshot = lambda entries, workspace: entries
    engine.git.validate = lambda entries, kind: [{"passed": True}]
    engine._step(engine.state.get_work(work["id"]))
    assert engine.state.get_work(work["id"])["phase"] == "reviewing"
    packet = engine._repair_packet(
        engine.state.get_work(work["id"]), "reviewer"
    )
    assert "skips tests" in packet["review_checklist"]
    assert "review_checklist" not in engine._repair_packet(
        engine.state.get_work(work["id"])
    )


def test_rejected_repair_review_consumes_budget_and_escalates(tmp_path):
    engine, gh = build(tmp_path)
    engine.config.pr_max_repairs = 2
    work = repair_work(engine, gh)
    target = ("plutoframework/cli", 5)
    item = engine.state.update_work(work["id"], phase="reviewing")
    before = get_pr(engine.state, *target)
    assert before["repairs"][SHA] == 1
    # Rejection spends the second of two attempts on this head.
    engine._repair_rejected(item, target, "removed an assertion")
    row = get_pr(engine.state, *target)
    assert row["repairs"][SHA] == 2 and row["repair_total"] == 2
    assert row["state"] == "needs_owner"
    assert engine.state.get_work(work["id"])["phase"] == "completed"
    assert list_decisions(engine.state, {"open"})


def test_rejected_repair_with_budget_left_revises(tmp_path):
    engine, gh = build(tmp_path)
    work = repair_work(engine, gh)
    target = ("plutoframework/cli", 5)
    item = engine.state.update_work(work["id"], phase="reviewing")
    engine._repair_rejected(item, target, "added a noqa")
    row = get_pr(engine.state, *target)
    assert row["repairs"][SHA] == 2 and row["state"] != "needs_owner"
    revised = engine.state.get_work(work["id"])
    assert revised["phase"] == "implementing"
    assert "added a noqa" in revised["data"]["revision_reason"]
