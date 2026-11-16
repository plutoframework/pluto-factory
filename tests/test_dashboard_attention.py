"""Needs-you projection and the single, narrow dashboard write path."""

import http.client
import json

import pytest
from test_dashboard import registered, server, submit

from pluto_factory.attention import add_decision, get_decision, set_pr, track_pr
from pluto_factory.dashboard_data import DashboardData
from pluto_factory.state import State


@pytest.fixture
def world(tmp_path):
    home = registered(tmp_path)
    state = State(home)
    work = submit(state)
    decision = add_decision(
        state,
        question="Pick one?",
        kind="choice",
        options=[
            {"label": "A", "recommended": True},
            {"label": "B"},
        ],
        work_id=work["id"],
    )
    row = track_pr(
        state,
        "owner/project",
        3,
        origin="tracked",
        title="Ready PR",
        url="https://github.com/owner/project/pull/3",
    )
    set_pr(state, row, state="ready", reason="ready", head="c" * 40)
    data = DashboardData(tmp_path)
    factory = data.state()["factories"][0]["id"]
    yield state, data, factory, decision
    state.close()


def post(serving, path, body=None, *, headers=None, raw=None):
    port = serving.server_address[1]
    origin = f"http://127.0.0.1:{port}"
    base = {
        "Origin": origin,
        "Sec-Fetch-Site": "same-origin",
        "Content-Type": "application/json",
        "X-Pluto-CSRF": serving.csrf,
    }
    for key, value in (headers or {}).items():
        if value is None:
            base.pop(key, None)
        else:
            base[key] = value
    payload = raw if raw is not None else json.dumps(body or {}).encode()
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
    connection.request("POST", path, body=payload, headers=base)
    response = connection.getresponse()
    result = response.status, response.read()
    connection.close()
    return result


def route(factory, decision):
    return f"/api/factories/{factory}/decisions/{decision['id']}/answer"


def test_state_lists_needs_you_with_csrf_token(world):
    _state, data, _factory, _decision = world
    with server(data) as serving:
        port = serving.server_address[1]
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
        connection.request("GET", "/api/state")
        payload = json.loads(connection.getresponse().read())
    assert payload["csrf"] == serving.csrf
    needs = payload["needs_you"]
    assert needs["count"] == 2
    assert needs["pull_requests"][0]["repo"] == "owner/project"
    assert needs["pull_requests"][0]["state"] == "ready"
    (shown,) = needs["decisions"]
    assert shown["question"] == "Pick one?"
    assert [o["recommended"] for o in shown["options"]] == [True, False]


def test_http_answer_records_only_the_answer(world):
    state, data, factory, decision = world
    with server(data) as serving:
        status, body = post(
            serving, route(factory, decision), {"answer": "A", "note": "ok"}
        )
    assert status == 200, body
    stored = get_decision(state, decision["id"])
    assert stored["state"] == "answered" and stored["answer"] == "A"
    kinds = [e["kind"] for e in state.inspect()["events"]]
    assert "decision.answered" in kinds and "decision.applied" not in kinds
    # The server never touches the blocked work.
    assert state.get_work(decision["work_id"])["control"] == "ready"
    with server(data) as serving:
        again, _ = post(serving, route(factory, decision), {"answer": "B"})
    assert again == 409


@pytest.mark.parametrize(
    "case,expected",
    [
        ("wrong-origin", 403),
        ("missing-origin", 403),
        ("cross-site", 403),
        ("no-fetch-metadata", 403),
        ("missing-csrf", 403),
        ("wrong-csrf", 403),
        ("form-content-type", 415),
        ("no-content-type", 415),
        ("oversize", 413),
        ("bad-json", 400),
        ("not-object", 400),
        ("extra-field", 400),
        ("bad-host", 403),
    ],
)
def test_write_path_refuses_untrusted_requests(world, case, expected):
    state, data, factory, decision = world
    headers, body, raw = {}, {"answer": "A"}, None
    if case == "wrong-origin":
        headers["Origin"] = "https://evil.example"
    elif case == "missing-origin":
        headers["Origin"] = None
    elif case == "cross-site":
        headers["Sec-Fetch-Site"] = "cross-site"
    elif case == "no-fetch-metadata":
        headers["Sec-Fetch-Site"] = None
    elif case == "missing-csrf":
        headers["X-Pluto-CSRF"] = None
    elif case == "wrong-csrf":
        headers["X-Pluto-CSRF"] = "nope"
    elif case == "form-content-type":
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    elif case == "no-content-type":
        headers["Content-Type"] = None
    elif case == "oversize":
        raw = json.dumps({"answer": "A" * 40000}).encode()
    elif case == "bad-json":
        raw = b"{nope"
    elif case == "not-object":
        raw = b'["A"]'
    elif case == "extra-field":
        body = {"answer": "A", "state": "applied"}
    elif case == "bad-host":
        headers["Host"] = "evil.example"
    with server(data) as serving:
        status, _ = post(
            serving, route(factory, decision), body, headers=headers, raw=raw
        )
    assert status == expected
    assert get_decision(state, decision["id"])["state"] == "open"


def test_unknown_factory_decision_and_routes_are_refused(world):
    state, data, factory, decision = world
    with server(data) as serving:
        missing = {"id": "decision-000000000000"}
        assert post(serving, route(factory, missing), {"answer": "A"})[0] == 404
        other = "f" * 20
        assert post(serving, route(other, decision), {"answer": "A"})[0] == 404
        for path in (
            "/api/state",
            f"/api/factories/{factory}/work/x",
            f"/api/factories/{factory}/decisions/{decision['id']}",
            route(factory, decision) + "?x=1",
            route(factory, decision) + "/",
            "/",
        ):
            assert post(serving, path, {"answer": "A"})[0] in {404, 405}, path
        assert get_decision(state, decision["id"])["state"] == "open"


def test_other_methods_stay_refused_and_get_is_unchanged(world):
    _state, data, factory, decision = world
    with server(data) as serving:
        port = serving.server_address[1]
        for method in ("PUT", "PATCH", "DELETE", "OPTIONS"):
            connection = http.client.HTTPConnection(
                "127.0.0.1", port, timeout=3
            )
            connection.request(
                method,
                route(factory, decision),
                headers={"Origin": f"http://127.0.0.1:{port}"},
            )
            assert connection.getresponse().status == 405
            connection.close()
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
        connection.request("GET", route(factory, decision))
        assert connection.getresponse().status == 404


def test_operator_context_is_the_visible_problem_line(world):
    state, data, _factory, _decision = world
    add_decision(
        state,
        question="Raise timeout?",
        context="Phase 2 times out on the big repo.\n\nTraceback: boom",
        kind="free_text",
    )
    shown = {d["question"]: d for d in data.state()["needs_you"]["decisions"]}[
        "Raise timeout?"
    ]
    assert shown["context"] == "Phase 2 times out on the big repo."
    assert shown["evidence"] == "Traceback: boom"
    # A decision with no extra evidence has none to collapse.
    add_decision(
        state, question="Plain?", context="Just this.", kind="free_text"
    )
    plain = {d["question"]: d for d in data.state()["needs_you"]["decisions"]}
    assert plain["Plain?"]["context"] == "Just this."
    assert plain["Plain?"]["evidence"] == ""


def test_worker_block_text_stays_collapsed_evidence(world):
    state, data, _factory, _decision = world
    decision = add_decision(
        state,
        question="Blocked on tests",
        context="RAW worker output",
        kind="free_text",
        advice="pending",
    )
    shown = {d["id"]: d for d in data.state()["needs_you"]["decisions"]}[
        decision["id"]
    ]
    assert shown["context"] == ""
    assert shown["evidence"] == "RAW worker output"


def test_config_decision_projects_key_value_and_approving_option(world):
    state, data, _factory, _decision = world
    decision = add_decision(
        state,
        question="Raise phase timeout?",
        kind="config",
        proposal={"key": "phase_timeout_seconds", "value": 3600},
    )
    shown = {d["id"]: d for d in data.state()["needs_you"]["decisions"]}[
        decision["id"]
    ]
    assert shown["proposal"] == {"key": "phase_timeout_seconds", "value": 3600}
    approves = {o["label"]: o["approves"] for o in shown["options"]}
    assert approves == {"Apply": True, "Decline": False}


def test_http_free_text_is_custom_except_for_config(world):
    state, data, factory, decision = world
    config = add_decision(
        state,
        question="Raise timeout?",
        kind="config",
        proposal={"key": "phase_timeout_seconds", "value": 3600},
    )
    with server(data) as serving:
        assert post(serving, route(factory, config), {"answer": "99"})[0] == 400
        status, _ = post(serving, route(factory, decision), {"answer": "Z"})
    assert status == 200
    assert get_decision(state, config["id"])["state"] == "open"
    answered = get_decision(state, decision["id"])
    assert answered["answer"] == "Z" and answered["custom"] == 1


def test_needs_you_lists_only_open_decisions(world):
    from pluto_factory.attention import (
        answer_decision,
        mark_applied,
        withdraw_decision,
    )

    state, data, _factory, decision = world
    others = [
        add_decision(
            state,
            question=f"Other {n}?",
            kind="choice",
            options=[{"label": "A", "recommended": True}, {"label": "B"}],
        )
        for n in range(3)
    ]
    answer_decision(state, others[0]["id"], "A")
    answer_decision(state, others[1]["id"], "A")
    mark_applied(state, others[1]["id"])
    withdraw_decision(state, others[2]["id"], "stale")
    needs = data.state()["needs_you"]
    assert [d["id"] for d in needs["decisions"]] == [decision["id"]]
    assert needs["count"] == 2  # one decision + the ready PR
    assert needs["auto"] == []
    assert get_decision(state, others[2]["id"])["note"] == "stale"


def test_auto_decisions_are_listed_apart_from_needs_you(world):
    import time

    from pluto_factory.attention import AUTO_BY, apply_due_autos

    state, data, _factory, decision = world
    auto = add_decision(
        state,
        question="Obvious?",
        kind="choice",
        options=[{"label": "Go", "recommended": True}, {"label": "No"}],
    )
    meta = {
        "category": "app_fix",
        "reason": "r",
        "answer": "Go",
        "apply_at": time.time() + 600,
        "status": "pending",
    }
    state.db.execute(
        "UPDATE decisions SET state='auto_pending',auto=? WHERE id=?",
        (json.dumps(meta), auto["id"]),
    )
    state.db.commit()
    needs = data.state()["needs_you"]
    assert [d["id"] for d in needs["decisions"]] == [decision["id"]]
    assert [d["id"] for d in needs["auto"]] == [auto["id"]]
    apply_due_autos(state, time.time() + 601)
    assert get_decision(state, auto["id"])["answered_by"] == AUTO_BY
    needs = data.state()["needs_you"]
    assert [d["id"] for d in needs["auto"]] == [auto["id"]]
    assert [d["id"] for d in needs["decisions"]] == [decision["id"]]


def test_advice_state_is_exposed_for_the_drawer(world):
    from pluto_factory.dashboard_data import _advice_state

    state, data, _factory, decision = world
    base = {"state": "open"}
    assert _advice_state({**base, "advice": "pending"}) == "analysing"
    assert _advice_state({**base, "advice": "fallback"}) == "unavailable"
    assert _advice_state({**base, "advice": "model"}) == "ready"
    assert _advice_state({**base, "advice": "none"}) == "none"
    state.db.execute(
        "UPDATE decisions SET advice='pending' WHERE id=?", (decision["id"],)
    )
    state.db.commit()
    (shown,) = data.state()["needs_you"]["decisions"]
    assert shown["advice_state"] == "analysing"
    state.db.execute(
        "UPDATE decisions SET advice='fallback' WHERE id=?", (decision["id"],)
    )
    state.db.commit()
    (shown,) = data.state()["needs_you"]["decisions"]
    assert shown["advice_state"] == "unavailable"


def test_drawer_disables_options_while_the_advisor_runs():
    from importlib.resources import files

    js = files("pluto_factory").joinpath("dashboard/app.js").read_text()
    assert "Advisor analysing…" in js and "Advisor unavailable" in js
    assert 'decision.advice_state === "analysing"' in js
    assert "button.disabled = analysing ||" in js
    # Custom answers stay available while analysing.
    assert "Send custom answer" in js
