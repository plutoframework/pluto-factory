"""Fix propagation chain: receiving follow-ups and dependent-side gating."""

import base64
import json
import subprocess
import time

from test_upstream import KEY, UPSTREAM, UpstreamGh, choose_framework, setup

from pluto_factory.config import write_config
from pluto_factory.engine import Engine
from pluto_factory.models import (
    CliPin,
    FactoryConfig,
    PropagationPolicy,
    PropagationRule,
    Repository,
    Submission,
)

FIX = "f" * 40
PIN_OLD = "1" * 40
CLI = "plutoframework/cli"


# --- receiving side -------------------------------------------------------


class Recv(UpstreamGh):
    def __init__(self):
        super().__init__()
        self.posted = []

    def __call__(self, argv, **kwargs):
        if argv[:3] == ["gh", "issue", "comment"]:
            self.posted.append(argv[argv.index("--body") + 1])
        if argv[:3] == ["gh", "issue", "view"] and "comments" in argv:
            self.calls.append(list(argv))
            body = json.dumps({"comments": [{"body": b} for b in self.posted]})
            return subprocess.CompletedProcess(argv, 0, body, "")
        return super().__call__(argv, **kwargs)


def receiver(tmp_path, rules=None):
    for name in ("auth", "cli"):
        (tmp_path / name).mkdir(exist_ok=True)
    config = FactoryConfig(
        name="pluto",
        harness="fixture",
        coordinator="rules",
        isolation="cooperative",
        repositories=[
            Repository(
                name=name,
                path=str(tmp_path / name),
                github=f"plutoframework/{name}",
                checks=[["true"]],
                qa=[["true"]],
            )
            for name in ("auth", "cli")
        ],
        propagation=PropagationPolicy(
            cli_repo="cli",
            rules=rules
            if rules is not None
            else [
                PropagationRule(
                    library="auth",
                    sibling_modules=["stores/postgres"],
                    cli_bump=True,
                )
            ],
        ),
    )
    home = tmp_path / "factory"
    write_config(home, config)
    engine = Engine(home)
    gh = Recv()
    engine.tracker.adapter.runner = gh
    work = engine.state.submit(
        Submission(
            title="Per-tenant OIDC",
            instruction="i",
            acceptance=["a"],
            repositories=["auth"],
            source="github:plutoframework/auth",
            delivery_key="5",
        )
    )
    now = time.time()
    engine.state.db.execute(
        "INSERT INTO triage_issues (slug,number,version,state,created,"
        "updated) VALUES ('plutoframework/auth',5,'v','accepted',?,?)",
        (now, now),
    )
    engine.state.db.commit()
    return engine, gh, work


def complete(engine, work, merge=FIX):
    data = {
        **engine.state.get_work(work["id"])["data"],
        "pull_requests": [{"repository": "auth", "merge_commit": merge}],
    }
    # Written directly: the delivery gate needs a real tracked PR.
    engine.state.db.execute(
        "UPDATE work SET phase='completed',control='settled',data=? WHERE id=?",
        (json.dumps(data), work["id"]),
    )
    engine.state.db.commit()


def test_completion_creates_the_chain_and_announces_the_final_item(tmp_path):
    engine, gh, work = receiver(tmp_path)
    assert engine.propagate() == 0  # not completed yet
    complete(engine, work)
    engine.propagate()
    items = {
        w["payload"]["delivery_key"]: w
        for w in engine.state.list_work()
        if w["payload"]["source"] == "propagation"
    }
    module = items[f"{work['id']}:module:stores/postgres"]
    cli = items[f"{work['id']}:cli"]
    assert module["payload"]["depends_on"] == [work["id"]]
    assert cli["payload"]["depends_on"] == [module["id"]]
    assert cli["payload"]["repositories"] == ["cli"]
    assert FIX in module["payload"]["instruction"]
    assert "regenerate goldens" in cli["payload"]["instruction"]
    final = f"propagation:{work['id']}:cli"
    assert gh.posted == [
        f"<!-- pluto-factory:propagation-final:{final} -->\n"
        + "Follow-up work queued so this fix is consumable downstream."
    ]
    # Idempotent.
    engine.propagate()
    assert len(gh.posted) == 1
    assert (
        len(
            [
                w
                for w in engine.state.list_work()
                if w["payload"]["source"] == "propagation"
            ]
        )
        == 2
    )
    # The done marker follows once the final item completes.
    engine.state.db.execute(
        "UPDATE work SET phase='completed' WHERE id=?", (cli["id"],)
    )
    engine.state.db.commit()
    engine.propagate()
    assert f"propagation-done:{final}" in gh.posted[-1]
    engine.propagate()
    assert len(gh.posted) == 2


def test_no_chain_for_cli_sources_untriaged_or_ruleless_libraries(tmp_path):
    engine, gh, work = receiver(tmp_path, rules=[])
    complete(engine, work)
    engine.propagate()
    assert not gh.posted and len(engine.state.list_work()) == 1


# --- dependent side -------------------------------------------------------


class Chain(UpstreamGh):
    """Scripted gh for the dependent-side checks."""

    def __init__(self):
        super().__init__()
        self.comments_on_issue = []
        self.pinned = PIN_OLD
        self.status = "diverged"
        self.cli_head = "c" * 40

    def __call__(self, argv, **kwargs):
        if argv[:3] == ["gh", "issue", "view"] and "comments" in argv:
            self.calls.append(list(argv))
            body = json.dumps(
                {"comments": [{"body": b} for b in self.comments_on_issue]}
            )
            return subprocess.CompletedProcess(argv, 0, body, "")
        if argv[:3] == ["gh", "pr", "view"]:
            out = json.dumps({"mergeCommit": {"oid": FIX}})
            return subprocess.CompletedProcess(argv, 0, out, "")
        if argv[:2] == ["gh", "api"]:
            path = argv[2]
            if path.startswith(f"repos/{CLI}/commits/"):
                out = {"sha": self.cli_head}
            elif path.startswith(f"repos/{CLI}/contents/"):
                assert path.endswith(f"ref={self.cli_head}")
                text = f'auth = "{self.pinned}"\n'
                out = {"content": base64.b64encode(text.encode()).decode()}
            elif "/compare/" in path:
                assert path == (
                    f"repos/{UPSTREAM}/compare/{FIX}...{self.pinned}"
                )
                out = {"status": self.status}
            else:
                out = {}
            return subprocess.CompletedProcess(argv, 0, json.dumps(out), "")
        return super().__call__(argv, **kwargs)


def dependent(tmp_path):
    engine, _, work = setup(
        tmp_path,
        mode="on_decision",
    )
    engine.config.framework.cli_repo = CLI
    engine.config.framework.cli_pins = {
        "pluto-core": CliPin(path="pins.txt", pattern=r'auth = "(\w+)"')
    }
    gh = Chain()
    engine.tracker.adapter.runner = gh
    choose_framework(engine)
    engine.flush_upstream()
    gh.created.clear()
    return engine, gh, work


CLOSED = {
    "state": "CLOSED",
    "stateReason": "COMPLETED",
    "closedByPullRequestsReferences": [
        {"number": 9, "url": f"https://github.com/{UPSTREAM}/pull/9"}
    ],
}


def poll(engine):
    engine.attention(force=True)
    row = engine.state.db.execute(
        "SELECT state,closing FROM external_issues WHERE key=?", (KEY,)
    ).fetchone()
    return row["state"], row["closing"]


def test_stays_blocked_until_the_pin_contains_the_fix(tmp_path):
    engine, gh, work = dependent(tmp_path)
    gh.remote[(UPSTREAM, 7)] = CLOSED
    state, closing = poll(engine)
    assert state == "awaiting_fix" and "not yet in the CLI's pinned" in closing
    assert not engine.state.dependency_status(work["id"])["ready"]
    assert not engine.state.guidance(work["id"])
    # The CLI moves to a pin that contains the fix.
    gh.pinned, gh.status = "a" * 40, "ahead"
    gh.cli_head = "d" * 40
    state, _ = poll(engine)
    assert state == "completed"
    assert engine.state.dependency_status(work["id"])["ready"]
    (guidance,) = engine.state.guidance(work["id"])
    assert "d" * 40 in guidance["text"] and "pluto generate" in guidance["text"]


def test_announced_propagation_must_be_done_first(tmp_path):
    engine, gh, work = dependent(tmp_path)
    gh.remote[(UPSTREAM, 7)] = CLOSED
    gh.pinned, gh.status = "a" * 40, "identical"
    gh.comments_on_issue = ["<!-- pluto-factory:propagation-final:k1 -->"]
    state, closing = poll(engine)
    assert state == "awaiting_fix" and "follow-up work is not done" in closing
    gh.comments_on_issue.append("<!-- pluto-factory:propagation-done:k1 -->")
    assert poll(engine)[0] == "completed"
    assert engine.state.dependency_status(work["id"])["ready"]


def test_unconfigured_chain_keeps_the_original_behaviour(tmp_path):
    engine, _gh, work = setup(tmp_path)
    gh2 = UpstreamGh()
    engine.tracker.adapter.runner = gh2
    choose_framework(engine)
    engine.flush_upstream()
    gh2.remote[(UPSTREAM, 7)] = CLOSED
    engine.attention(force=True)
    assert engine.state.dependency_status(work["id"])["ready"]
