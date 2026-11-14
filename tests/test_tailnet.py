import http.client
import json
import subprocess
import threading
from contextlib import contextmanager

import pytest

from pluto_factory import tailnet
from pluto_factory.dashboard_data import DashboardData
from pluto_factory.dashboard_server import DashboardServer
from pluto_factory.tailnet import TailnetAccess, TailnetError, discover

NAME = "desk.example.ts.net"
OWNER = "owner@github"


@contextmanager
def server(tmp_path, access):
    with DashboardServer(DashboardData(tmp_path), 0, tailnet=access) as live:
        thread = threading.Thread(target=live.serve_forever, daemon=True)
        thread.start()
        try:
            yield live
        finally:
            live.shutdown()
            thread.join(2)


def response(live, headers):
    connection = http.client.HTTPConnection(
        "127.0.0.1", live.server_address[1], timeout=3
    )
    connection.putrequest("GET", "/api/state", skip_host=True)
    for key, value in headers:
        connection.putheader(key, value)
    connection.endheaders()
    reply = connection.getresponse()
    body = json.loads(reply.read())
    connection.close()
    return reply.status, body


def status(live, headers):
    connection = http.client.HTTPConnection(
        "127.0.0.1", live.server_address[1], timeout=3
    )
    connection.putrequest("GET", "/api/state", skip_host=True)
    for key, value in headers:
        connection.putheader(key, value)
    connection.endheaders()
    response = connection.getresponse()
    response.read()
    connection.close()
    return response.status


def test_tailnet_requests_need_allowed_login_and_tailnet_host(tmp_path):
    access = TailnetAccess(NAME, 443, frozenset({OWNER}))
    with server(tmp_path, access) as live:
        local = f"127.0.0.1:{live.server_address[1]}"
        assert status(live, [("Host", local)]) == 200
        allowed = [("Host", NAME), ("Tailscale-User-Login", OWNER)]
        assert status(live, allowed) == 200
        assert status(live, [*allowed, ("Origin", f"https://{NAME}")]) == 200
        # No identity, a stranger, duplicated identity, wrong origin scheme.
        assert status(live, [("Host", NAME)]) == 403
        stranger = [("Host", NAME), ("Tailscale-User-Login", "x@github")]
        assert status(live, stranger) == 403
        doubled = [*allowed, ("Tailscale-User-Login", "x@github")]
        assert status(live, doubled) == 403
        assert status(live, [*allowed, ("Origin", f"http://{NAME}")]) == 403
        other = [("Host", "other.ts.net"), ("Tailscale-User-Login", OWNER)]
        assert status(live, other) == 403


def test_tailnet_host_is_refused_when_tailscale_is_off(tmp_path):
    with server(tmp_path, None) as live:
        allowed = [("Host", NAME), ("Tailscale-User-Login", OWNER)]
        assert status(live, allowed) == 403


def test_non_default_port_requires_port_in_host(tmp_path):
    access = TailnetAccess(NAME, 8443, frozenset({OWNER}))
    assert access.url == f"https://{NAME}:8443/"
    with server(tmp_path, access) as live:
        login = ("Tailscale-User-Login", OWNER)
        assert status(live, [("Host", f"{NAME}:8443"), login]) == 200
        assert status(live, [("Host", NAME), login]) == 403


def fake_tailscale(monkeypatch, status_value, serve_value=None):
    def run(arguments, **_options):
        if arguments[1:] == ["status", "--json"]:
            return subprocess.CompletedProcess(
                arguments, 0, json.dumps(status_value), ""
            )
        return subprocess.CompletedProcess(
            arguments, 0, json.dumps(serve_value or {}), ""
        )

    monkeypatch.setattr(tailnet.shutil, "which", lambda _: "/bin/tailscale")
    monkeypatch.setattr(tailnet.subprocess, "run", run)


STATUS = {
    "BackendState": "Running",
    "Self": {"DNSName": NAME + ".", "UserID": 7},
    "User": {"7": {"LoginName": OWNER}},
    "CertDomains": [NAME],
}


def test_discover_defaults_to_node_owner_plus_allowed(monkeypatch):
    fake_tailscale(monkeypatch, STATUS)
    access = discover(443, ["teammate@github"])
    assert access.dns_name == NAME
    assert access.logins == {OWNER, "teammate@github"}
    assert access.hosts == {NAME, f"{NAME}:443"}


def test_discover_refuses_used_port_and_missing_https(monkeypatch):
    fake_tailscale(monkeypatch, STATUS, {"TCP": {"443": {"HTTPS": True}}})
    with pytest.raises(TailnetError, match="already uses HTTPS port 443"):
        discover(443, [])
    fake_tailscale(monkeypatch, {**STATUS, "CertDomains": []})
    with pytest.raises(TailnetError, match="HTTPS certificates"):
        discover(443, [])
    fake_tailscale(monkeypatch, {**STATUS, "BackendState": "Stopped"})
    with pytest.raises(TailnetError, match="not connected"):
        discover(443, [])


def test_refusals_name_the_failed_check(tmp_path, capsys):
    access = TailnetAccess(NAME, 443, frozenset({OWNER}))
    with server(tmp_path, access) as live:
        code, body = response(
            live, [("Host", NAME), ("Tailscale-User-Login", "x@github")]
        )
        assert code == 403
        assert "'x@github' is not allowed" in body["error"]
        assert "--tailscale-allow x@github" in body["error"]
        _, body = response(live, [("Host", "100.1.2.3")])
        assert "not this tailnet name" in body["error"]
        _, body = response(live, [("Host", NAME)])
        assert "Tailscale-User-Login" in body["error"]
    assert "refused request" in capsys.readouterr().err
