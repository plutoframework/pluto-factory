"""'No changes to publish': complete verifiably-satisfied items, else ask."""

import base64
import json
import subprocess

from test_advisor import make_work_with_issue
from test_attention import Gh, build

from pluto_factory.attention import answer_decision, list_decisions
from pluto_factory.models import Failure, Submission

NO_CHANGES = Failure(
    code="github.no_changes",
    summary="No changes to publish; reconcile prior merges",
    source="adapter",
)
TARGET = "f" * 40
PINNED = "a" * 40


class ModGh(Gh):
    def __init__(self, status):
        super().__init__()
        self.status = status

    def __call__(self, argv, **kwargs):
        if argv[:2] == ["gh", "api"]:
            path = argv[2]
            if path.endswith("/go.mod"):
                text = f"require github.com/owner/core v0.0.0-20260101000000-{PINNED[:12]}\n"
                out = {"content": base64.b64encode(text.encode()).decode()}
            elif "/compare/" in path:
                assert f"{TARGET}...{PINNED[:12]}" in path
                out = {"status": self.status}
            else:
                out = {}
            return subprocess.CompletedProcess(argv, 0, json.dumps(out), "")
        return super().__call__(argv, **kwargs)


def chain(engine):
    """An origin work item plus its propagation follow-up."""
    origin = make_work_with_issue(engine)
    data = {
        **origin["data"],
        "pull_requests": [{"repository": "core", "merge_commit": TARGET}],
    }
    engine.state.db.execute(
        "UPDATE work SET phase='completed',data=? WHERE id=?",
        (json.dumps(data), origin["id"]),
    )
    engine.state.db.commit()
    return engine.state.submit(
        Submission(
            title="Pin stores/postgres to the auth fix",
            instruction="i",
            acceptance=["a"],
            repositories=["core"],
            source="propagation",
            delivery_key=f"{origin['id']}:module:stores/postgres",
            depends_on=[origin["id"]],
        )
    )


def test_verifiably_satisfied_item_completes_and_the_chain_continues(tmp_path):
    engine, _ = build(tmp_path)
    engine.tracker.adapter.runner = ModGh("ahead")
    item = chain(engine)
    engine._failure(item["id"], NO_CHANGES)
    done = engine.state.get_work(item["id"])
    assert done["phase"] == "completed" and done["control"] == "settled"
    assert "contains" in done["data"]["already_satisfied"]
    kinds = [e["kind"] for e in engine.state.inspect()["events"]]
    assert "work.already_satisfied" in kinds
    assert not list_decisions(engine.state)  # no fallback decision
    # A dependent item is now unblocked.
    follow = engine.state.submit(
        Submission(
            title="CLI bump",
            instruction="i",
            acceptance=["a"],
            repositories=["core"],
            depends_on=[item["id"]],
        )
    )
    assert engine.state.dependency_status(follow["id"])["ready"]


def test_unverifiable_item_goes_to_the_advisor_with_a_completing_option(
    tmp_path,
):
    engine, _ = build(tmp_path)
    engine.tracker.adapter.runner = ModGh("diverged")  # not an ancestor
    item = chain(engine)
    engine._failure(item["id"], NO_CHANGES)
    assert engine.state.get_work(item["id"])["control"] == "deferred"
    (decision,) = list_decisions(engine.state, {"open"})
    assert decision["advice"] == "pending"  # the advisor still rewrites it
    assert "already have landed" in decision["context"]
    first = decision["options"][0]
    assert first["completes"] and first["recommended"]
    answer_decision(engine.state, decision["id"], first["label"])
    engine.process_decisions()
    done = engine.state.get_work(item["id"])
    assert done["phase"] == "completed" and done["control"] == "settled"
    assert "Owner confirmed" in done["data"]["already_satisfied"]


def test_other_items_never_auto_complete(tmp_path):
    engine, _ = build(tmp_path)
    engine.tracker.adapter.runner = ModGh("ahead")
    work = make_work_with_issue(engine)
    engine._failure(work["id"], NO_CHANGES)
    assert engine.state.get_work(work["id"])["phase"] != "completed"
    assert list_decisions(engine.state, {"open"})
