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
        ("not-an-option", 400),
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
    elif case == "not-an-option":
        body = {"answer": "Z"}
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
