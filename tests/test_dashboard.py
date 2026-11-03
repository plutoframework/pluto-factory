"""The dashboard observes live ledgers without writes or remote actions."""

import hashlib
import http.client
import json
import subprocess
import threading
from contextlib import contextmanager

import pytest

from pluto_factory.cli import main
from pluto_factory.config import write_config
from pluto_factory.dashboard_data import DashboardData, text
from pluto_factory.dashboard_server import DashboardServer
from pluto_factory.models import FactoryConfig, Repository, Submission
from pluto_factory.state import State
from pluto_factory.visibility import VisibilityError


def registered(root, name="test", integration="managed"):
    home = root / name
    (root / "checkout").mkdir(parents=True, exist_ok=True)
    write_config(
        home,
        FactoryConfig(
            name=name,
            integration=integration,
            repositories=[
                Repository(
                    name="app",
                    path=str(root / "checkout"),
                    github="owner/project",
                )
            ],
        ),
    )
    return home


def submit(state, title="Example", **values):
    return state.submit(
        Submission(
            title=title,
            instruction="Implement the request",
            acceptance=["The feature works"],
            repositories=["app"],
            **values,
        )
    )


def files(home):
    return {
        str(p.relative_to(home)): (
            hashlib.sha256(p.read_bytes()).hexdigest(),
            p.stat().st_mtime_ns,
        )
        for p in home.rglob("*")
        if p.is_file()
    }


@contextmanager
def server(data):
    with DashboardServer(data, port=0) as serving:
        thread = threading.Thread(target=serving.serve_forever, daemon=True)
        thread.start()
        try:
            yield serving
        finally:
            serving.shutdown()
            thread.join(2)


def request(serving, path="/api/state", *, method="GET", headers=None):
    port = serving.server_address[1]
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
    connection.request(method, path, headers=headers or {})
    response = connection.getresponse()
    result = response.status, dict(response.getheaders()), response.read()
    connection.close()
    return result


def test_live_wal_snapshot_detail_is_read_only_and_allowlisted(tmp_path):
    home = registered(tmp_path)
    state = State(home)
    completed = submit(state, "Foundation")
    state.update_work(completed["id"], phase="completed")
    work = submit(
        state,
        "<script>alert('task')</script>",
        depends_on=[completed["id"]],
        source="github:owner/project",
        delivery_key="3",
    )
    state.update_work(
        work["id"],
        data={
            "revision_reason": "Please revise",
            "pull_requests": [
                {
                    "repository": "app",
                    "number": 4,
                    "url": "https://github.com/owner/project/pull/4",
                    "state": "open",
                }
            ],
            "secret_arbitrary_data": "DO-NOT-EXPOSE",
        },
    )
    run = state.create_run(
        work["id"],
        "implementer",
        {"id": "run-one", "prompt": "DO-NOT-EXPOSE", "argv": ["DO-NOT-EXPOSE"]},
    )
    state.update_run(
        run["id"], state="running", session={"argv": ["DO-NOT-EXPOSE"]}
    )
    with state.db:
        state.db.execute(
            "UPDATE runs SET report=? WHERE id=?",
            (
                json.dumps(
                    {
                        "outcome": "ready",
                        "summary": "Done sk-abcdefghijklmno123456",
                        "remaining_work": [],
                        "artifacts": [],
                        "private": "DO-NOT-EXPOSE",
                    }
                ),
                run["id"],
            ),
        )
    state.event(
        "failure.observed",
        work["id"],
        {
            "summary": "Provider failed xoxb-1234567890-abcd",
            "private": "DO-NOT-EXPOSE",
        },
    )
    state.evidence(
        work["id"],
        {
            "kind": "checks",
            "repository": "app",
            "passed": True,
            "command": ["python", "-m", "unittest"],
            "environment": {"FEATURE_SNAKE": "0", "API_KEY": "DO-NOT-EXPOSE"},
            "exit_code": 0,
            "stdout": "ok ghp_abcdefghijklmnop123456",
            "stderr": "",
            "seconds": 0.1,
            "config_digest": "DO-NOT-EXPOSE",
        },
    )
    before = files(home)
    data = DashboardData(tmp_path)
    summary = data.state()
    fid = summary["factories"][0]["id"]
    detail = data.detail(fid, work["id"])
    assert detail["work"]["dependencies"] == [
        {"id": completed["id"], "title": "Foundation", "state": "completed"}
    ]
    assert detail["work"]["source_url"].endswith("/issues/3")
    assert detail["work"]["pr_links"][0]["url"].endswith("/pull/4")
    assert detail["work"]["active_role"] == "implementer"
    assert detail["work"]["instruction"] == "Implement the request"
    assert (
        detail["work"]["evidence"][0]["environment"]["API_KEY"] == "[REDACTED]"
    )
    assert detail["work"]["herdr"]["terminal_command"].endswith(" --terminal")
    encoded = json.dumps(detail)
    assert "DO-NOT-EXPOSE" not in encoded
    assert "sk-abcdefgh" not in encoded and "ghp_abc" not in encoded
    assert "xoxb-123" not in encoded
    assert "[REDACTED]" in encoded
    assert files(home) == before
    state.close()


def test_missing_registry_and_registered_factory_never_initialize(tmp_path):
    root = tmp_path / "missing"
    assert DashboardData(root).state()["factories"] == []
    assert not root.exists()
    home = registered(tmp_path)
    before = files(home)
    data = DashboardData(tmp_path)
    entry = data.state()["factories"][0]
    assert entry["status"] == "registered" and entry["work"] == []
    with pytest.raises(KeyError):
        data.detail(entry["id"], "absent")
    assert files(home) == before
    assert not (home / "state.sqlite3").exists()


def test_watcher_running_independent_of_paused_work(tmp_path):
    home = registered(tmp_path, integration="github")
    state = State(home)
    work = submit(state)
    state.pause(work["id"])
    with state.writer():
        entry = DashboardData(tmp_path).state()["factories"][0]
        assert entry["coordinator_running"] is True
        assert entry["paused"] is False
        assert entry["watcher"] == {"enabled": True, "state": "running"}
        assert entry["work"][0]["control"] == "paused"
    state.close()


def test_closed_database_snapshot_creates_no_source_sidecars(tmp_path):
    home = registered(tmp_path)
    state = State(home)
    submit(state)
    state.close()
    before = files(home)
    DashboardData(tmp_path).state()
    assert files(home) == before
    assert not (home / "state.sqlite3-wal").exists()
    assert not (home / "state.sqlite3-shm").exists()


def test_broken_factory_does_not_hide_healthy_factory(tmp_path):
    registered(tmp_path, "healthy")
    bad = registered(tmp_path, "broken")
    (bad / "state.sqlite3").write_bytes(b"broken SQLite")
    entries = DashboardData(tmp_path).state()["factories"]
    assert len(entries) == 2
    assert next(x for x in entries if x["name"] == "broken")["errors"]


def test_http_boundaries_json_xss_and_methods(tmp_path):
    home = registered(tmp_path)
    state = State(home)
    submit(state, "<script>alert('title')</script>")
    with server(DashboardData(tmp_path)) as serving:
        status, headers, body = request(serving)
        assert status == 200
        assert headers["Content-Type"].startswith("application/json")
        assert b"<script>" not in body and b"\\u003cscript" in body
        assert "Access-Control-Allow-Origin" not in headers
        assert headers["X-Content-Type-Options"] == "nosniff"
        assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]
        for hostile in (
            {"Host": "evil.example"},
            {"Origin": "https://evil.example"},
            {"Origin": "null"},
            {"Sec-Fetch-Site": "cross-site"},
        ):
            assert request(serving, headers=hostile)[0] == 403
        for path in (
            "/../factory.toml",
            "/%2e%2e/factory.toml",
            "/api/state?home=/etc",
            "/dashboard/../../fnox.toml",
            "/api/factories/../../work/a",
            "/favicon.ico",
        ):
            assert request(serving, path)[0] == 404
        for method in ("POST", "PUT", "PATCH", "DELETE", "OPTIONS"):
            assert request(serving, method=method)[0] == 405
        port = serving.server_address[1]
        assert (
            request(serving, headers={"Origin": f"http://127.0.0.1:{port}"})[0]
            == 200
        )
    state.close()


def test_detail_http_cannot_resolve_arbitrary_home(tmp_path):
    home = registered(tmp_path)
    state = State(home)
    work = submit(state)
    data = DashboardData(tmp_path)
    fid = data.state()["factories"][0]["id"]
    with server(data) as serving:
        assert (
            request(serving, f"/api/factories/{fid}/work/{work['id']}")[0]
            == 200
        )
        assert (
            request(serving, f"/api/factories/{'0' * 20}/work/{work['id']}")[0]
            == 404
        )
    state.close()


def test_redaction_bounds_private_keys_and_terminal_escapes():
    value = (
        "\x1b[31mred\x1b[0m\n-----BEGIN OPENSSH PRIVATE KEY-----\nopaque\n-----END OPENSSH PRIVATE KEY-----\nBearer abcdefghijklmnopqrstuvwxyz\n"
        + "a" * 20000
    )
    output = text(value, 1000)
    assert (
        "opaque" not in output
        and "Bearer" not in output
        and "\x1b" not in output
    )
    assert output.endswith("[truncated]")


def test_ui_cli_wiring_inherits_global_and_local_home(monkeypatch, tmp_path):
    from pluto_factory import dashboard_server

    calls = []
    monkeypatch.setattr(
        dashboard_server,
        "serve",
        lambda root, **options: (
            calls.append((root, options)) or {"stopped": True}
        ),
    )
    assert (
        main(
            [
                "ui",
                "--root",
                str(tmp_path),
                "--home",
                str(tmp_path / "one"),
                "--port",
                "8900",
            ]
        )
        == 0
    )
    assert calls[-1] == (
        tmp_path,
        {"extra_home": tmp_path / "one", "port": 8900},
    )
    assert (
        main(["--home", str(tmp_path / "two"), "ui", "--root", str(tmp_path)])
        == 0
    )
    assert calls[-1][1]["extra_home"] == tmp_path / "two"


def active_worker(state):
    work = submit(state)
    run = state.create_run(
        work["id"], "implementer", {"id": "owned-assignment"}
    )
    session = {
        "kind": "pi",
        "factory": "test",
        "work_id": work["id"],
        "assignment_id": run["id"],
        "generation": run["generation"],
        "role": "implementer",
        "owned": True,
        "core_identity": "core-one",
        "address": "t-one",
    }
    state.update_run(run["id"], state="running", session=session)
    return work, session


def core_runner(session, *, wrong_owner=False):
    calls = []

    def run(argv, **options):
        calls.append(argv)
        assert not options.get("shell")
        if argv[:3] == ["hc", "server", "status"]:
            value = {"core": {"id": "core-one"}}
        elif argv[:2] == ["hc", "get"]:
            value = {
                "state": "live",
                "address": "t-one",
                "labels": {
                    "factory": "test",
                    "work": "wrong" if wrong_owner else session["work_id"],
                    "assignment": session["assignment_id"],
                    "generation": str(session["generation"]),
                    "role": "implementer",
                },
            }
        else:
            assert argv == ["hc", "read", "--lines", "120", "t-one"]
            options["stdout"].write(b"Working sk-abcdefghijklmno123456")
            return subprocess.CompletedProcess(argv, 0)
        return subprocess.CompletedProcess(argv, 0, json.dumps(value), "")

    return run, calls


def test_terminal_read_is_lazy_owned_bounded_and_redacted(tmp_path):
    home = registered(tmp_path)
    state = State(home)
    work, session = active_worker(state)
    data = DashboardData(tmp_path)
    fid = data.state()["factories"][0]["id"]
    runner, calls = core_runner(session)
    before = files(home)
    captured = data.terminal(fid, work["id"], runner=runner)
    assert captured["available"] is True
    assert captured["text"] == "Working [REDACTED]"
    assert captured["attach_command"] == "hc attach t-one"
    assert files(home) == before
    assert len(calls) == 3
    state.pause(work["id"])
    assert not data.terminal(
        fid,
        work["id"],
        runner=lambda *_a, **_k: pytest.fail("read paused worker"),
    )["available"]
    state.close()


def test_terminal_wrong_owner_never_reads(tmp_path):
    home = registered(tmp_path)
    state = State(home)
    work, session = active_worker(state)
    data = DashboardData(tmp_path)
    fid = data.state()["factories"][0]["id"]
    runner, calls = core_runner(session, wrong_owner=True)
    with pytest.raises(VisibilityError, match="ownership changed"):
        data.terminal(fid, work["id"], runner=runner)
    assert all(argv[:2] != ["hc", "read"] for argv in calls)
    state.close()


def test_detail_corruption_returns_service_error_without_backend_exception(
    tmp_path,
):
    import sqlite3

    class InvalidData:
        def detail(self, *_arguments):
            raise sqlite3.OperationalError("PRIVATE DATABASE DIAGNOSTIC")

    with server(InvalidData()) as serving:
        status, _headers, content = request(
            serving, "/api/factories/" + "a" * 20 + "/work/one"
        )
        assert status == 503
        assert b"PRIVATE DATABASE DIAGNOSTIC" not in content


def test_duplicate_host_headers_and_absolute_uri_are_rejected(tmp_path):
    with server(DashboardData(tmp_path)) as serving:
        connection = http.client.HTTPConnection(
            "127.0.0.1", serving.server_address[1], timeout=3
        )
        connection.putrequest("GET", "/api/state")
        connection.putheader("Host", "evil.example")
        connection.endheaders()
        response = connection.getresponse()
        assert response.status == 403
        response.read()
        connection.close()
        assert (
            request(
                serving,
                "http://evil.example/api/state",
                headers={"Host": f"127.0.0.1:{serving.server_address[1]}"},
            )[0]
            == 404
        )


def test_detail_copy_commands_bind_selected_work_and_do_not_open_other_task(
    tmp_path,
):
    import shlex

    home = registered(tmp_path)
    state = State(home)
    old = submit(state, "Old completed task")
    state.update_work(old["id"], phase="completed")
    active, _session = active_worker(state)
    data = DashboardData(tmp_path)
    fid = data.state()["factories"][0]["id"]
    previous = data.detail(fid, old["id"])["work"]["herdr"]
    current = data.detail(fid, active["id"])["work"]["herdr"]
    assert shlex.split(previous["open_command"])[-2:] == ["--work", old["id"]]
    assert previous["available"] is False
    assert current["available"] is True
    state.close()


def test_malformed_work_payload_does_not_hide_healthy_factory(tmp_path):
    registered(tmp_path, "healthy")
    broken = registered(tmp_path, "malformed")
    state = State(broken)
    work = submit(state)
    with state.db:
        state.db.execute(
            "UPDATE work SET payload=? WHERE id=?",
            (json.dumps({"depends_on": None}), work["id"]),
        )
    entries = DashboardData(tmp_path).state()["factories"]
    assert len(entries) == 2
    assert (
        next(entry for entry in entries if entry["name"] == "healthy")["work"]
        == []
    )
    assert next(entry for entry in entries if entry["name"] == "malformed")[
        "errors"
    ]
    state.close()


def test_model_deferral_projects_actual_reason_without_failure_event(tmp_path):
    home = registered(tmp_path)
    state = State(home)
    work = submit(state, "Validated candidate")
    reason = "Acceptance is not yet demonstrated by the recorded evidence."
    state.event(
        "coordinator.decision",
        work["id"],
        {
            "decision": {"action": "defer", "rationale": reason},
            "prompt": "PRIVATE PROMPT",
        },
    )
    state.update_work(
        work["id"],
        phase="integrating",
        control="deferred",
        event="work.deferred",
        notice={
            "kind": "action_required",
            "message": reason,
            "incident_key": "deferred-test",
        },
    )
    data = DashboardData(tmp_path)
    entry = data.state()["factories"][0]
    assert entry["work"][0]["latest_failure"] == reason
    detail = data.detail(entry["id"], work["id"])["work"]
    assert detail["latest_failure"] == reason
    assert (
        next(
            event
            for event in detail["events"]
            if event["kind"] == "coordinator.decision"
        )["message"]
        == reason
    )
    assert (
        next(
            event
            for event in detail["events"]
            if event["kind"] == "work.deferred"
        )["message"]
        == reason
    )
    assert "PRIVATE PROMPT" not in json.dumps(detail)
    state.close()


@pytest.mark.parametrize(
    "phase,control",
    [
        ("completed", "ready"),
        ("implementing", "active"),
        ("checking", "ready"),
        ("awaiting_merge", "backoff"),
    ],
)
def test_historical_resolved_failure_is_not_current_attention(
    tmp_path, phase, control
):
    home = registered(tmp_path)
    state = State(home)
    work = submit(state)
    state.event(
        "failure.observed", work["id"], {"summary": "Old credential error"}
    )
    state.notify(
        work["id"], "action_required", "Old blocked reason", "old-incident"
    )
    state.update_work(work["id"], phase=phase, control=control)
    data = DashboardData(tmp_path)
    entry = data.state()["factories"][0]
    assert entry["work"][0]["latest_failure"] is None
    detail = data.detail(entry["id"], work["id"])["work"]
    assert detail["latest_failure"] is None
    assert any(
        event["message"] == "Old credential error" for event in detail["events"]
    )
    state.close()


def test_worker_heartbeats_do_not_displace_lifecycle_activity(tmp_path):
    home = registered(tmp_path)
    state = State(home)
    work = submit(state)
    state.event("work.prepared", work["id"], {"summary": "Worktree ready"})
    state.event(
        "worker.dispatched", work["id"], {"summary": "Implementation started"}
    )
    for sequence in range(150):
        state.event("worker.observed", work["id"], {"sequence": sequence})
    data = DashboardData(tmp_path)
    fid = data.state()["factories"][0]["id"]
    detail = data.detail(fid, work["id"])
    kinds = [event["kind"] for event in detail["work"]["events"]]
    assert "work.prepared" in kinds and "worker.dispatched" in kinds
    assert "worker.observed" not in kinds
    assert (
        state.db.execute(
            "SELECT COUNT(*) FROM events WHERE kind='worker.observed'"
        ).fetchone()[0]
        == 150
    )
    state.close()


@pytest.mark.parametrize(
    "filename", ["manrope.ttf", "bricolage-grotesque.ttf", "ibm-plex-mono.ttf"]
)
def test_packaged_font_routes_serve_only_whitelisted_local_assets(
    tmp_path, filename
):
    from importlib.resources import files

    font = files("pluto_factory").joinpath("dashboard", "fonts", filename)
    assert font.is_file(), "Required bundled font must be packaged locally"
    with server(DashboardData(tmp_path)) as serving:
        status, headers, body = request(serving, "/fonts/" + filename)
        assert status == 200
        assert headers["Content-Type"] == "font/ttf"
        assert "font-src 'self'" in headers["Content-Security-Policy"]
        assert body == font.read_bytes() and len(body) > 1000
        for path in (
            "/fonts/../factory.toml",
            "/fonts/unknown.ttf",
            "/fonts/manrope-OFL.txt",
        ):
            assert request(serving, path)[0] == 404


def observed_agent(
    state, *, observation=None, age=0, run_state="running", phase="implementing"
):
    import time

    work, session = active_worker(state)
    state.update_work(work["id"], phase=phase)
    state.update_run(session["assignment_id"], state=run_state)
    if observation is not None:
        data = {"run_id": session["assignment_id"], **observation}
        state.event("worker.observed", work["id"], data)
        with state.db:
            state.db.execute(
                "UPDATE events SET created=? WHERE seq=(SELECT MAX(seq) FROM events)",
                (time.time() - age,),
            )
    return work, session


def test_motion_uses_fresh_working_current_assignment_in_summary_and_detail(
    tmp_path,
):
    home = registered(tmp_path)
    state = State(home)
    work, _ = observed_agent(state, observation={"state": "working"}, age=2)
    before = files(home)
    data = DashboardData(tmp_path)
    with state.writer():
        entry = data.state()["factories"][0]
        summary = entry["work"][0]
        detail = data.detail(entry["id"], work["id"])["work"]
        for view in (summary, detail):
            assert view["agent_working"] is True
            assert view["agent_state"] == "working"
            assert view["agent_observed_at"] is not None
        assert all(
            event["kind"] != "worker.observed" for event in detail["events"]
        )
    # Acquiring the observer lock above creates it; projections create no files.
    after = files(home)
    before.pop("coordinator.lock", None)
    after.pop("coordinator.lock", None)
    assert after == before
    state.close()


@pytest.mark.parametrize(
    "run_state,observation,age,phase,expected",
    [
        ("starting", None, 0, "implementing", "starting"),
        ("running", None, 0, "implementing", "unknown"),
        ("running", {"state": "working"}, 0, "queued", "queued"),
        (
            "running",
            {"state": "working", "startup_pending": True},
            0,
            "implementing",
            "starting",
        ),
        ("running", {"state": "working"}, 20, "implementing", "stale"),
        ("running", {"state": "working"}, -20, "implementing", "stale"),
        ("running", {"state": "settled"}, 0, "implementing", "settled"),
        ("running", {"state": "finished"}, 0, "implementing", "finished"),
        ("running", {"state": "idle"}, 0, "implementing", "idle"),
        ("running", {"state": "blocked"}, 0, "implementing", "blocked"),
        (
            "report_received",
            {"state": "working"},
            0,
            "implementing",
            "report_received",
        ),
        ("settled", {"state": "working"}, 0, "implementing", "settled"),
        ("running", {"state": "working"}, 0, "completed", "finished"),
    ],
)
def test_motion_does_not_infer_work_from_assignment_or_stale_observation(
    tmp_path, run_state, observation, age, phase, expected
):
    home = registered(tmp_path)
    state = State(home)
    observed_agent(
        state,
        observation=observation,
        age=age,
        run_state=run_state,
        phase=phase,
    )
    with state.writer():
        summary = DashboardData(tmp_path).state()["factories"][0]["work"][0]
    assert summary["agent_working"] is False
    assert summary["agent_state"] == expected
    state.close()


@pytest.mark.parametrize(
    "control", ["paused", "deferred", "quarantined", "backoff"]
)
def test_paused_or_blocked_work_never_moves(tmp_path, control):
    home = registered(tmp_path)
    state = State(home)
    work, _ = observed_agent(state, observation={"state": "working"})
    state.update_work(work["id"], control=control)
    with state.writer():
        summary = DashboardData(tmp_path).state()["factories"][0]["work"][0]
    assert summary["agent_working"] is False
    assert summary["agent_state"] == control
    state.close()


def test_paused_factory_or_stopped_observer_never_claims_agent_is_working(
    tmp_path,
):
    home = registered(tmp_path)
    state = State(home)
    observed_agent(state, observation={"state": "working"})
    data = DashboardData(tmp_path)
    stopped = data.state()["factories"][0]["work"][0]
    assert stopped["agent_working"] is False
    assert stopped["agent_state"] == "observer_stopped"
    state.pause()
    with state.writer():
        paused = data.state()["factories"][0]["work"][0]
    assert paused["agent_working"] is False
    assert paused["agent_state"] == "paused"
    state.close()


def test_latest_observation_for_old_run_never_animates_current_assignment(
    tmp_path,
):
    home = registered(tmp_path)
    state = State(home)
    work, _session = observed_agent(state, observation={"state": "working"})
    # A late report from an earlier assignment supersedes the latest valid
    # observation; the reader must not search backward for a working event.
    state.event(
        "worker.observed",
        work["id"],
        {"run_id": "old-assignment", "state": "working"},
    )
    with state.writer():
        summary = DashboardData(tmp_path).state()["factories"][0]["work"][0]
    assert summary["agent_working"] is False
    assert summary["agent_observed_at"] is None
    state.close()


def test_queued_work_without_an_assignment_stays_static(tmp_path):
    home = registered(tmp_path)
    state = State(home)
    submit(state)
    with state.writer():
        summary = DashboardData(tmp_path).state()["factories"][0]["work"][0]
    assert summary["agent_working"] is False
    assert summary["agent_state"] == "queued"
    assert summary["agent_observed_at"] is None
    state.close()
