"""A deferred item always has someone acting on it."""

import time

from test_advisor import make_work_with_issue
from test_attention import build

from pluto_factory.attention import list_decisions, withdraw_decision
from pluto_factory.models import AutonomyPolicy, Submission

KEY = "github:plutoframework/pluto-core:7"


def deferred(engine):
    work = make_work_with_issue(engine)
    engine._defer(engine.state.get_work(work["id"]), "Re-pin libraries")
    return work


def orphan_events(engine):
    return [
        e
        for e in engine.state.inspect()["events"]
        if e["kind"] == "work.orphan_deferred"
    ]


def drop_decisions(engine):
    engine.state.db.execute("DELETE FROM decisions")
    engine.state.db.commit()


def test_orphan_without_history_gets_a_decision(tmp_path):
    engine, _ = build(tmp_path)
    work = deferred(engine)
    drop_decisions(engine)  # the real case: nothing tracks the deferral
    assert engine.sweep_orphans() == [work["id"]]
    (decision,) = list_decisions(engine.state, {"open"})
    assert decision["work_id"] == work["id"] and decision["advice"] == "pending"
    assert "Re-pin libraries" in decision["context"]
    assert orphan_events(engine)
    assert engine.state.get_work(work["id"])["control"] == "deferred"
    assert engine.sweep_orphans() == []  # now it is owned


def test_resolved_decision_resumes_under_autonomy(tmp_path):
    engine, _ = build(tmp_path)
    work = deferred(engine)
    (decision,) = list_decisions(engine.state, {"open"})
    withdraw_decision(engine.state, decision["id"], "stale")  # via the CLI
    engine.attention(force=True)
    resumed = engine.state.get_work(work["id"])
    assert resumed["control"] == "ready"
    assert any(
        "Resumed automatically" in g["text"]
        for g in engine.state.guidance(work["id"])
    )
    (event,) = orphan_events(engine)
    assert event["subject"] == work["id"]


def test_ask_all_mode_asks_instead_of_resuming(tmp_path):
    engine, _ = build(tmp_path, autonomy=AutonomyPolicy(mode="ask_all"))
    work = deferred(engine)
    (decision,) = list_decisions(engine.state, {"open"})
    withdraw_decision(engine.state, decision["id"])
    engine.sweep_orphans()
    assert engine.state.get_work(work["id"])["control"] == "deferred"
    (fresh,) = list_decisions(engine.state, {"open"})
    assert fresh["id"] != decision["id"]


def test_owned_deferrals_are_left_alone(tmp_path):
    engine, _ = build(tmp_path)
    work = deferred(engine)  # an open decision
    assert engine.sweep_orphans() == []
    drop_decisions(engine)
    engine.state.db.execute(
        "UPDATE work SET wake_at=? WHERE id=?",
        (time.time() + 600, work["id"]),
    )
    engine.state.db.commit()
    assert engine.sweep_orphans() == []  # a scheduled wake
    engine.state.db.execute("UPDATE work SET wake_at=0")
    engine.state.db.commit()
    engine.state.add_dependency_source(work["id"], KEY)
    assert engine.sweep_orphans() == []  # a tracked external dependency
    engine.state.db.execute(
        "INSERT INTO external_issues (key,slug,number,state) "
        "VALUES (?,?,?,'completed')",
        (KEY, "plutoframework/pluto-core", 7),
    )
    engine.state.db.commit()
    assert engine.sweep_orphans() == [work["id"]]  # dependency met: orphan


def test_unmet_work_dependency_counts_as_acting(tmp_path):
    engine, _ = build(tmp_path)
    first = engine.state.submit(
        Submission(
            title="first",
            instruction="i",
            acceptance=["a"],
            repositories=["core"],
        )
    )
    work = deferred(engine)
    drop_decisions(engine)
    engine.state.add_dependency(work["id"], first["id"])
    assert engine.sweep_orphans() == []


def test_applied_decision_without_resume_resolves_the_item(tmp_path):
    engine, _ = build(tmp_path)
    work = deferred(engine)
    (decision,) = list_decisions(engine.state, {"open"})
    # Applied with no guidance/resume (a path that forgot the item).
    engine.state.db.execute(
        "UPDATE decisions SET state='applied',answer='x' WHERE id=?",
        (decision["id"],),
    )
    engine.state.db.commit()
    engine.attention(force=True)
    assert engine.state.get_work(work["id"])["control"] == "ready"
