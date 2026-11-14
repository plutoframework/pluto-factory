"""Adapter boundaries are tested without contacting models or real terminals."""

import json
import os
import subprocess
from pathlib import Path

import pytest

from pluto_factory.config import atomic_json
from pluto_factory.harness import FixtureHarness, HarnessError, PiHarness
from pluto_factory.models import FactoryConfig, Repository, WorkerReport


class FakeCore:
    def __init__(self, terminals=None, wait=None):
        self.terminals = terminals or []
        self.wait = wait or {
            "ended": True,
            "status": "done",
            "reason": "status",
        }
        self.calls = []
        self.next_launch_error = None

    def __call__(self, args, **_kwargs):
        self.calls.append(args)
        if args[0] != "hc":
            return subprocess.CompletedProcess(
                args, 0, '{"status":"ready","provider":"test"}', ""
            )
        command = args[1]
        if command == "ls":
            value = {"terminals": self.terminals}
        elif command == "server":
            value = {"core": {"id": "test-core"}}
        elif command == "run":
            if self.next_launch_error:
                raise self.next_launch_error
            value = {"address": "t7"}
        elif command == "wait":
            value = self.wait
        elif command == "get":
            address = args[-1]
            value = next(t for t in self.terminals if t["address"] == address)
        else:
            value = {"observed": True}
        return subprocess.CompletedProcess(args, 0, json.dumps(value), "")


def assignment(tmp_path, *, identifier="assignment-one", role="implementer"):
    root = tmp_path / "repo"
    root.mkdir(exist_ok=True)
    folder = tmp_path / "assignments" / identifier
    folder.mkdir(parents=True, exist_ok=True)
    values = {
        "id": identifier,
        "work_id": "work-one",
        "generation": 1,
        "role": role,
        "home": str(tmp_path),
        "directory": str(root),
        "roots": [{"name": "sample", "path": str(root), "access": "write"}],
        "session_dir": str(folder / "session"),
        "assignment_path": str(folder / "assignment.json"),
        "packet_path": str(folder / "task.md"),
        "role_path": str(folder / "role.md"),
        "schema_path": str(folder / "schema.json"),
        "report_path": str(folder / "report.json"),
        "started_path": str(folder / "started.json"),
        "status_path": str(folder / "status.json"),
    }
    Path(values["packet_path"]).write_text("A task with `shell` $syntax")
    Path(values["role_path"]).write_text("Role context")
    Path(values["schema_path"]).write_text(
        json.dumps(WorkerReport.model_json_schema())
    )
    return values


def config(tmp_path, *, isolation="cooperative", herdr_space=""):
    return FactoryConfig(
        name="test",
        repositories=[Repository(name="sample", path=str(tmp_path))],
        isolation=isolation,
        herdr_space=herdr_space,
    )


def terminal(item):
    return {
        "address": "t7",
        "state": "live",
        "status": {"state": "done"},
        "labels": {
            "factory": "test",
            "assignment": item["id"],
            "generation": str(item["generation"]),
        },
    }


def test_pi_launch_uses_argv_initial_task_and_labels(tmp_path):
    core = FakeCore()
    item = assignment(tmp_path)
    session = PiHarness(config(tmp_path, herdr_space="space-one"), core).launch(
        item
    )
    call = next(call for call in core.calls if call[1] == "run")
    assert "--bg" in call
    assert "--no-parent" in call
    assert "--json" in call
    assert "herdr.space=space-one" in call
    assert f"assignment={item['id']}" in call
    assert f"@{item['packet_path']}" == call[-1]
    assert f"PATH={os.environ.get('PATH', os.defpath)}" in call
    assert "--print" not in call
    assert "--mode" not in call
    assert session["core_identity"] == "test-core"
    assert (
        json.loads(Path(item["assignment_path"]).read_text())["id"]
        == item["id"]
    )


def test_launch_reconciles_existing_assignment_including_finished(tmp_path):
    item = assignment(tmp_path)
    found = terminal(item)
    found["state"] = "finished"
    core = FakeCore([found])
    session = PiHarness(config(tmp_path), core).launch(item)
    assert session["address"] == "t7"
    assert not any(call[1] == "run" for call in core.calls)
    assert "--all" in core.calls[0]


def test_required_isolation_is_never_silently_downgraded(tmp_path):
    core = FakeCore()
    with pytest.raises(HarnessError, match="required isolation"):
        PiHarness(config(tmp_path, isolation="required"), core).launch(
            assignment(tmp_path)
        )
    assert not core.calls


def test_launch_timeout_is_uncertain_and_not_repeated(tmp_path):
    core = FakeCore()
    core.next_launch_error = subprocess.TimeoutExpired("hc", 15)
    with pytest.raises(HarnessError) as caught:
        PiHarness(config(tmp_path), core).launch(assignment(tmp_path))
    assert caught.value.failure["uncertain"]
    assert sum(call[1] == "run" for call in core.calls) == 1


def test_core_os_error_is_a_safe_typed_failure(tmp_path):
    def runner(_args, **_kwargs):
        raise PermissionError("sensitive OS diagnostic")

    harness = PiHarness(config(tmp_path), runner)
    with pytest.raises(HarnessError) as caught:
        harness._hc(["run", "--json"], mutating=True)
    assert caught.value.failure["code"] == "transport.unavailable"
    assert caught.value.failure["uncertain"]
    assert "sensitive" not in caught.value.failure["summary"]


def test_reviewer_has_no_shell_or_writing_tools(tmp_path):
    core = FakeCore()
    PiHarness(config(tmp_path), core).launch(
        assignment(tmp_path, role="reviewer")
    )
    args = next(call for call in core.calls if call[1] == "run")
    tools = args[args.index("--tools") + 1].split(",")
    assert "factory_report" in tools
    assert not {"bash", "edit", "write"}.intersection(tools)
    assert "--no-mcp" in args
    assert "--no-extensions" in args


def test_wait_timeout_is_working_not_worker_failure(tmp_path):
    item = assignment(tmp_path)
    core = FakeCore(
        wait={"ended": False, "reason": "timeout", "status": "working"}
    )
    harness = PiHarness(config(tmp_path), core)
    session = harness.launch(item)
    assert harness.observe(session, item) == {"state": "working"}
    assert any(call[1] == "wait" for call in core.calls)


def test_initial_idle_is_not_completion_before_task_start(tmp_path):
    item = assignment(tmp_path)
    core = FakeCore()
    harness = PiHarness(config(tmp_path), core)
    session = harness.launch(item)
    assert harness.observe(session, item) == {
        "state": "working",
        "startup_pending": True,
    }
    atomic_json(
        Path(item["started_path"]),
        {
            "assignment_id": "stale-assignment",
            "generation": 1,
            "task_started": True,
        },
    )
    assert harness.observe(session, item)["startup_pending"]
    atomic_json(
        Path(item["started_path"]),
        {
            "assignment_id": item["id"],
            "generation": item["generation"],
            "task_started": True,
        },
    )
    assert harness.observe(session, item) == {"state": "settled"}


def test_native_auth_readiness_never_requests_or_returns_credentials(tmp_path):
    core = FakeCore()
    harness = PiHarness(config(tmp_path), core)
    result = harness.readiness(tmp_path)
    call = core.calls[-1]
    assert result == {
        "status": "ready",
        "provider": config(tmp_path).worker.provider,
        "reason": "",
    }
    assert "--no-refresh" in call
    assert "--credentials" not in call
    assert "print-api-key" not in call
    assert "print-bearer-token" not in call


def test_missing_native_auth_does_not_create_a_terminal(tmp_path):
    core = FakeCore()

    def runner(args, **kwargs):
        if args[0] != "hc":
            core.calls.append(args)
            return subprocess.CompletedProcess(
                args,
                1,
                '{"status":"not_ready","reason":"credentials_not_configured"}',
                "",
            )
        return core(args, **kwargs)

    harness = PiHarness(config(tmp_path), runner)
    with pytest.raises(HarnessError) as caught:
        harness.launch(assignment(tmp_path))
    assert caught.value.failure["code"] == "auth.required"
    assert not any(call[1] == "run" for call in core.calls)


def test_native_readiness_whitelists_metadata_and_preserves_oauth_type(
    tmp_path,
):
    def runner(args, **_kwargs):
        return subprocess.CompletedProcess(
            args,
            0,
            json.dumps(
                {
                    "status": "ready",
                    "authType": "oauth",
                    "credential": "must-not-escape",
                    "accessToken": "must-not-escape",
                }
            ),
            "",
        )

    result = PiHarness(config(tmp_path), runner).readiness(tmp_path)
    assert result["auth_type"] == "oauth"
    assert "credential" not in result
    assert "accessToken" not in result


def test_native_readiness_rejects_malformed_metadata(tmp_path):
    def runner(args, **_kwargs):
        return subprocess.CompletedProcess(
            args, 0, '{"status":{"unexpected":"object"}}', ""
        )

    result = PiHarness(config(tmp_path), runner).readiness(tmp_path)
    assert result["status"] == "invalid"


def test_pi_launch_forwards_selected_runtime_directories(tmp_path, monkeypatch):
    core = FakeCore()
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(tmp_path / "native-config"))
    monkeypatch.setenv(
        "PI_CODING_AGENT_SESSION_DIR", str(tmp_path / "native-sessions")
    )
    PiHarness(config(tmp_path), core).launch(assignment(tmp_path))
    call = next(call for call in core.calls if call[1] == "run")
    assert f"PI_CODING_AGENT_DIR={tmp_path / 'native-config'}" in call
    assert f"PI_CODING_AGENT_SESSION_DIR={tmp_path / 'native-sessions'}" in call


def test_core_identity_change_cannot_reuse_terminal(tmp_path):
    item = assignment(tmp_path)
    core = FakeCore()
    harness = PiHarness(config(tmp_path), core)
    session = harness.launch(item)
    session["core_identity"] = "old-core"
    result = harness.observe(session, item)
    assert result["state"] == "unknown"
    assert result["failure"]["uncertain"]
    assert not any(call[1] == "wait" for call in core.calls)


def test_cleanup_refuses_a_terminal_whose_ownership_changed(tmp_path):
    item = assignment(tmp_path)
    found = terminal(item)
    core = FakeCore([found])
    harness = PiHarness(config(tmp_path), core)
    session = harness.launch(item)
    found["labels"]["assignment"] = "another-assignment"
    with pytest.raises(HarnessError, match="ownership changed"):
        harness.close(session)
    assert not any(call[1] in {"kill", "rm"} for call in core.calls)


def test_fixture_fixes_only_registered_roots_and_reports(tmp_path):
    item = assignment(tmp_path)
    root = Path(item["directory"])
    (root / "calculator.py").write_text("return left - right\n")
    (root / "factory_fixture.json").write_text(
        json.dumps(
            {
                "file": "calculator.py",
                "replacements": [
                    {"old": "left - right", "new": "left + right"}
                ],
            }
        )
    )
    harness = FixtureHarness(config(tmp_path))
    session = harness.launch(item)
    assert harness.observe(session, item)["state"] == "settled"
    assert (root / "calculator.py").read_text() == "return left + right\n"
    wrapper = json.loads(Path(item["report_path"]).read_text())
    assert wrapper["assignment_id"] == item["id"]
    assert WorkerReport.model_validate(wrapper["report"]).outcome == "ready"
    assert harness.find_existing(item) == session


@pytest.mark.parametrize(
    "scenario",
    [
        "crash_once",
        "malformed_once",
        "provider_once",
        "idle_no_report_once",
    ],
)
def test_fixture_failure_once_is_per_work_not_assignment(tmp_path, scenario):
    first = assignment(tmp_path, identifier="first")
    first["fixture_scenario"] = scenario
    harness = FixtureHarness(config(tmp_path))
    harness.launch(first)
    second = assignment(tmp_path, identifier="second")
    second["fixture_scenario"] = scenario
    harness.launch(second)
    wrapper = json.loads(Path(second["report_path"]).read_text())
    assert WorkerReport.model_validate(wrapper["report"]).outcome == "ready"


def test_fixture_recipe_cannot_escape_root(tmp_path):
    item = assignment(tmp_path)
    outside = tmp_path / "outside.py"
    outside.write_text("untouched")
    root = Path(item["directory"])
    (root / "factory_fixture.json").write_text(
        json.dumps(
            {
                "file": "../outside.py",
                "replacements": [{"old": "untouched", "new": "changed"}],
            }
        )
    )
    with pytest.raises(HarnessError, match="escaped"):
        FixtureHarness(config(tmp_path)).launch(item)
    assert outside.read_text() == "untouched"


def test_read_only_fixture_root_is_not_modified(tmp_path):
    item = assignment(tmp_path)
    item["roots"][0]["access"] = "read"
    root = Path(item["directory"])
    (root / "calculator.py").write_text("old")
    (root / "factory_fixture.json").write_text(
        json.dumps(
            {
                "file": "calculator.py",
                "replacements": [{"old": "old", "new": "new"}],
            }
        )
    )
    FixtureHarness(config(tmp_path)).launch(item)
    assert (root / "calculator.py").read_text() == "old"


class RebindingCore(FakeCore):
    def __init__(self, terminals=None):
        super().__init__(terminals)
        self.current = None
        self.lose_task_response = False
        self.lose_bind_response = False

    def __call__(self, args, **kwargs):
        if args[1] == "send":
            self.calls.append(args)
            message = args[-1]
            if message.startswith("/factory-bind "):
                if self.lose_bind_response:
                    raise subprocess.TimeoutExpired("hc", 15)
                request = json.loads(message.removeprefix("/factory-bind "))
                self.current = json.loads(
                    Path(request["assignment_path"]).read_text()
                )
                atomic_json(
                    Path(self.current["started_path"]),
                    {
                        "assignment_id": self.current["id"],
                        "generation": self.current["generation"],
                        "bound": True,
                        "task_started": False,
                    },
                )
                value = {"started": False}
            else:
                atomic_json(
                    Path(self.current["started_path"]),
                    {
                        "assignment_id": self.current["id"],
                        "generation": self.current["generation"],
                        "task_started": True,
                    },
                )
                if self.lose_task_response:
                    raise subprocess.TimeoutExpired("hc", 15)
                value = {"started": True}
            return subprocess.CompletedProcess(args, 0, json.dumps(value), "")
        if args[1] == "label":
            self.calls.append(args)
            for label in args[4:]:
                key, value = label.split("=", 1)
                self.terminals[0]["labels"][key] = value
            return subprocess.CompletedProcess(args, 0, "{}", "")
        return super().__call__(args, **kwargs)


def test_reassign_binds_before_task_and_keeps_terminal(tmp_path):
    first = assignment(tmp_path, identifier="first")
    core = RebindingCore([terminal(first)])
    harness = PiHarness(config(tmp_path), core)
    session = harness.launch(first)
    second = assignment(tmp_path, identifier="second")
    second["generation"] = 2
    updated = harness.reassign(session, second)
    sends = [call for call in core.calls if call[1] == "send"]
    assert sends[0][-1].startswith("/factory-bind ")
    assert sends[1][-1] == f"@{second['packet_path']}"
    assert updated["address"] == session["address"]
    assert updated["assignment_id"] == "second"
    assert updated["generation"] == 2


def test_uncertain_revision_send_is_not_replayed(tmp_path):
    first = assignment(tmp_path, identifier="first")
    core = RebindingCore([terminal(first)])
    harness = PiHarness(config(tmp_path), core)
    session = harness.launch(first)
    second = assignment(tmp_path, identifier="second")
    second["generation"] = 2
    core.lose_task_response = True
    with pytest.raises(HarnessError) as caught:
        harness.reassign(session, second)
    assert caught.value.failure["uncertain"]
    core.terminals[0]["status"] = {"state": "working"}
    updated = harness.reassign(session, second)
    assert updated["assignment_id"] == "second"
    task_sends = [
        call
        for call in core.calls
        if call[1] == "send" and call[-1].startswith("@")
    ]
    assert len(task_sends) == 1


def test_uncertain_binding_without_ack_is_not_replayed(tmp_path):
    first = assignment(tmp_path, identifier="first")
    core = RebindingCore([terminal(first)])
    harness = PiHarness(config(tmp_path), core)
    session = harness.launch(first)
    second = assignment(tmp_path, identifier="second")
    second["generation"] = 2
    core.lose_bind_response = True
    for _ in range(2):
        with pytest.raises(HarnessError) as caught:
            harness.reassign(session, second)
        assert caught.value.failure["uncertain"]
    assert sum(call[1] == "send" for call in core.calls) == 1


def test_reassign_changed_role_requires_fresh_session(tmp_path):
    first = assignment(tmp_path, identifier="first")
    core = RebindingCore([terminal(first)])
    harness = PiHarness(config(tmp_path), core)
    session = harness.launch(first)
    second = assignment(tmp_path, identifier="second")
    second["generation"] = 2
    Path(second["role_path"]).write_text("Changed role")
    with pytest.raises(HarnessError, match="Changed role"):
        harness.reassign(session, second)
    assert not any(call[1] == "send" for call in core.calls)


def test_reassign_changed_model_requires_fresh_session(tmp_path):
    first = assignment(tmp_path, identifier="first")
    core = RebindingCore([terminal(first)])
    harness = PiHarness(config(tmp_path), core)
    session = harness.launch(first)
    second = assignment(tmp_path, identifier="second")
    second["generation"] = 2
    harness.config["worker"]["model"] = "a-different-model"
    with pytest.raises(HarnessError, match="Changed model"):
        harness.reassign(session, second)
    assert not any(call[1] == "send" for call in core.calls)


def test_native_extension_persists_reports_and_rebinds(tmp_path):
    first = assignment(tmp_path, identifier="first")
    atomic_json(Path(first["assignment_path"]), first)
    second = assignment(tmp_path, identifier="second")
    second["generation"] = 2
    atomic_json(Path(second["assignment_path"]), second)
    extension = Path(__file__).parents[1] / "src/pluto_factory/pi_extension.ts"
    script = r"""
import fs from "node:fs";
import { pathToFileURL } from "node:url";
const [extension, firstPath, nextPath] = process.argv.slice(1);
process.env.PLUTO_FACTORY_ASSIGNMENT = firstPath;
const tools = {}, commands = {}, events = {};
const pi = {
  registerTool(tool) { tools[tool.name] = tool; },
  registerCommand(name, command) { commands[name] = command; },
  on(name, callback) { events[name] = callback; },
};
const ctx = {
  isIdle() { return true; },
  hasPendingMessages() { return false; },
  sessionManager: { getSessionFile() { return "native-session.jsonl"; } },
  model: { id: "test-model" },
};
const module = await import(pathToFileURL(extension).href);
module.default(pi);
await events.session_start({}, ctx);
await events.agent_start({}, ctx);
const first = JSON.parse(fs.readFileSync(firstPath));
const result = await tools.factory_report.execute("report-one", {
  outcome: "ready", summary: "First result",
});
if (!result.terminate) throw new Error("Missing terminating hint");
const blocked = await events.tool_call({ toolName: "write" });
if (!blocked.block) throw new Error("Writes remained enabled after report");
await events.agent_settled({ aborted: false });
await commands["factory-bind"].handler(JSON.stringify({
  assignment_path: nextPath,
}), ctx);
await events.agent_start({}, ctx);
await tools.factory_report.execute("report-two", {
  outcome: "ready", summary: "Second result",
});
const next = JSON.parse(fs.readFileSync(nextPath));
const report = JSON.parse(fs.readFileSync(next.report_path));
if (report.assignment_id !== next.id || report.generation !== 2) {
  throw new Error("Report identity was not rebound");
}
let rejected = false;
try {
  await commands["factory-bind"].handler(JSON.stringify({
    assignment_path: nextPath,
  }), ctx);
} catch { rejected = true; }
if (!rejected) throw new Error("Stale generation was accepted");
console.log(JSON.stringify({ first: first.id, second: next.id }));
"""
    result = subprocess.run(
        [
            "node",
            "--input-type=module",
            "-e",
            script,
            str(extension),
            first["assignment_path"],
            second["assignment_path"],
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=15,
    )
    assert json.loads(result.stdout) == {"first": "first", "second": "second"}


def test_native_extension_records_trace_when_enabled(tmp_path):
    traced = assignment(tmp_path, identifier="traced")
    raw = tmp_path / "trace.raw.jsonl"
    traced["trace"] = {"path": str(raw), "max_output_bytes": 32}
    atomic_json(Path(traced["assignment_path"]), traced)
    extension = Path(__file__).parents[1] / "src/pluto_factory/pi_extension.ts"
    script = r"""
import { pathToFileURL } from "node:url";
const [extension, path] = process.argv.slice(1);
process.env.PLUTO_FACTORY_ASSIGNMENT = path;
const events = {};
const pi = {
  registerTool() {}, registerCommand() {},
  on(name, callback) { events[name] = callback; },
};
const module = await import(pathToFileURL(extension).href);
module.default(pi);
const result = await events.tool_result({
  toolName: "bash", toolCallId: "c1", isError: false,
  input: { command: "pluto add endpoint" },
  content: [{ type: "text", text: "x".repeat(100) }],
  details: { exitCode: 0 },
});
if (result !== undefined) throw new Error("Trace must not alter results");
await events.message_end({ message: {
  role: "assistant", model: "m", usage: { input: 3, output: 4 },
  content: [{ type: "text", text: "Done" }],
} });
await events.message_end({ message: { role: "user", content: [] } });
"""
    subprocess.run(
        [
            "node",
            "--input-type=module",
            "-e",
            script,
            str(extension),
            traced["assignment_path"],
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=15,
    )
    events = [json.loads(line) for line in raw.read_text().splitlines()]
    assert [event["kind"] for event in events] == ["tool", "assistant.message"]
    tool, message = events
    assert tool["tool"] == "bash" and tool["exit_code"] == 0
    assert tool["input"]["command"] == "pluto add endpoint"
    assert "truncated" in tool["output"]
    assert message["text"] == "Done" and message["usage"]["output"] == 4
    assert raw.stat().st_mode & 0o777 == 0o600
