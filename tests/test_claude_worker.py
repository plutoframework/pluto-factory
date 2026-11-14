"""Claude Code harness and its MCP/hook reporting bridge, without a model."""

import io
import json
import os
from pathlib import Path

import pytest
from test_harness import FakeCore, assignment, terminal

from pluto_factory import claude_worker
from pluto_factory.harness import ClaudeHarness, HarnessError, make_harness
from pluto_factory.models import FactoryConfig, Repository


class ClaudeCore(FakeCore):
    def __call__(self, args, **kwargs):
        if args[0] != "hc":
            import subprocess

            self.calls.append(args)
            value = {"loggedIn": True, "authMethod": "claude.ai"}
            return subprocess.CompletedProcess(args, 0, json.dumps(value), "")
        return super().__call__(args, **kwargs)


def config(tmp_path, **extra):
    return FactoryConfig(
        name="test",
        repositories=[Repository(name="sample", path=str(tmp_path))],
        isolation="cooperative",
        harness="claude",
        **extra,
    )


def launch(tmp_path, role="implementer", **extra):
    core = ClaudeCore()
    item = assignment(tmp_path, role=role)
    session = ClaudeHarness(config(tmp_path, **extra), core).launch(item)
    call = next(call for call in core.calls if call[1] == "run")
    return item, session, call


def test_make_harness_selects_claude(tmp_path):
    assert isinstance(make_harness(config(tmp_path)), ClaudeHarness)


def test_claude_launch_command(tmp_path):
    item, session, call = launch(tmp_path)
    assert session["kind"] == "claude"
    native = call[call.index("claude") :]
    assert native[-1] == Path(item["packet_path"]).read_text()
    assert native[-2] == "Role context"
    assert native[-3] == "--append-system-prompt"
    assert native[native.index("--model") + 1] == "gpt-6.1-sol"
    assert native[native.index("--effort") + 1] == "high"
    assert native[native.index("--permission-mode") + 1] == "dontAsk"
    assert native[native.index("--setting-sources") + 1] == "user"
    assert "--strict-mcp-config" in native
    tools = native[native.index("--tools") + 1].split(",")
    assert {"Bash", "Edit", "Write"} <= set(tools)
    allowed = native[native.index("--allowedTools") + 1].split(",")
    assert claude_worker.QUALIFIED_TOOL in allowed
    folder = Path(item["assignment_path"]).parent
    mcp = json.loads((folder / "claude-mcp.json").read_text())
    server = mcp["mcpServers"]["factory"]
    assert server["env"]["PLUTO_FACTORY_ASSIGNMENT"] == item["assignment_path"]
    settings = json.loads((folder / "claude-settings.json").read_text())
    assert {"SessionStart", "UserPromptSubmit", "PreToolUse", "Stop"} <= set(
        settings["hooks"]
    )


def test_claude_reviewer_is_read_only_and_trust_loads_project(tmp_path):
    _item, _session, call = launch(
        tmp_path, role="reviewer", trust_registered_repositories=True
    )
    tools = call[call.index("--tools") + 1].split(",")
    assert not {"Bash", "Edit", "Write"}.intersection(tools)
    assert call[call.index("--setting-sources") + 1] == "user,project,local"


def test_claude_unknown_effort_is_omitted(tmp_path):
    profile = {"provider": "anthropic", "model": "m", "reasoning": "off"}
    _item, _session, call = launch(tmp_path, worker=profile)
    assert "--effort" not in call


@pytest.mark.parametrize(
    ("value", "status", "auth"),
    [
        ({"loggedIn": True, "authMethod": "claude.ai"}, "ready", "oauth"),
        ({"loggedIn": False, "authMethod": "api_key"}, "ready", "api_key"),
        ({"loggedIn": False, "authMethod": "none"}, "not_ready", "ambient"),
    ],
)
def test_claude_readiness(tmp_path, value, status, auth):
    import subprocess

    def runner(args, **_kwargs):
        assert args[-4:] == ["claude", "auth", "status", "--json"]
        return subprocess.CompletedProcess(args, 0, json.dumps(value), "")

    result = ClaudeHarness(config(tmp_path), runner).readiness(tmp_path)
    assert result["status"] == status
    assert result["auth_type"] == auth


def test_claude_reassign_closes_and_requires_fresh_session(tmp_path):
    item = assignment(tmp_path)
    core = ClaudeCore([terminal(item)])
    harness = ClaudeHarness(config(tmp_path), core)
    session = harness.launch(item)
    with pytest.raises(HarnessError) as caught:
        harness.reassign(session, item)
    assert caught.value.failure["code"] == "config.fresh_session_required"
    assert not caught.value.failure["uncertain"]
    assert any(call[1] == "kill" for call in core.calls)


def report(**extra):
    return {"outcome": "ready", "summary": "done", **extra}


def test_mcp_lists_and_records_report(tmp_path):
    item = assignment(tmp_path)
    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "factory_report", "arguments": report()},
        },
        {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {
                "name": "factory_report",
                "arguments": report(summary="other"),
            },
        },
    ]
    stdin = io.StringIO("".join(json.dumps(r) + "\n" for r in requests))
    stdout = io.StringIO()
    os.environ["PLUTO_FACTORY_ASSIGNMENT"] = str(Path(item["assignment_path"]))
    Path(item["assignment_path"]).write_text(json.dumps(item))
    try:
        claude_worker.serve(stdin, stdout)
    finally:
        del os.environ["PLUTO_FACTORY_ASSIGNMENT"]
    responses = [json.loads(line) for line in stdout.getvalue().splitlines()]
    assert [r["id"] for r in responses] == [1, 2, 3, 4]
    assert responses[1]["result"]["tools"][0]["name"] == "factory_report"
    assert "isError" not in responses[2]["result"]
    assert responses[3]["result"]["isError"] is True
    envelope = json.loads(Path(item["report_path"]).read_text())
    assert envelope["assignment_id"] == item["id"]
    assert envelope["report"]["summary"] == "done"
    status = json.loads(Path(item["status_path"]).read_text())
    assert status["state"] == "reported"


def test_mcp_rejects_invalid_report(tmp_path):
    item = assignment(tmp_path)
    response = claude_worker.handle_message(
        item,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "factory_report", "arguments": {"x": 1}},
        },
    )
    assert response["result"]["isError"] is True
    assert not Path(item["report_path"]).exists()


def test_hooks_track_start_and_require_report(tmp_path):
    item = assignment(tmp_path)
    hook = claude_worker.handle_hook
    assert hook(item, "SessionStart", {"source": "startup"}) is None
    started = json.loads(Path(item["started_path"]).read_text())
    assert started["task_started"] is False
    hook(item, "UserPromptSubmit", {})
    started = json.loads(Path(item["started_path"]).read_text())
    assert started["task_started"] is True
    hook(item, "SessionStart", {"source": "compact"})
    started = json.loads(Path(item["started_path"]).read_text())
    assert started["task_started"] is True
    assert hook(item, "Stop", {})["decision"] == "block"
    assert hook(item, "Stop", {"stop_hook_active": True}) is None
    assert hook(item, "PreToolUse", {"tool_name": "Bash"}) is None
    claude_worker.record_report(item, report())
    denied = hook(item, "PreToolUse", {"tool_name": "Bash"})
    assert denied["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert (
        hook(item, "PreToolUse", {"tool_name": claude_worker.QUALIFIED_TOOL})
        is None
    )
    assert hook(item, "Stop", {}) is None
    status = json.loads(Path(item["status_path"]).read_text())
    assert status["state"] == "settled"


def _claude_config(folder):
    return json.loads((folder / ".claude.json").read_text())


def test_trusted_launch_pre_accepts_workspace_trust(
    tmp_path, isolated_claude_config
):
    path = isolated_claude_config / ".claude.json"
    path.write_text(
        json.dumps({"keep": 1, "projects": {"/other": {"x": True}}})
    )
    path.chmod(0o600)
    item, _session, _call = launch(tmp_path, trust_registered_repositories=True)
    data = _claude_config(isolated_claude_config)
    directory = str(Path(item["directory"]).resolve())
    assert data["projects"][directory]["hasTrustDialogAccepted"] is True
    assert data["keep"] == 1
    assert data["projects"]["/other"] == {"x": True}
    assert path.stat().st_mode & 0o777 == 0o600


def test_untrusted_launch_leaves_claude_config_alone(
    tmp_path, isolated_claude_config
):
    launch(tmp_path)
    assert not (isolated_claude_config / ".claude.json").exists()


def test_trust_covers_git_worktree_main_repository(
    tmp_path, isolated_claude_config
):
    import subprocess

    from pluto_factory.harness import trust_claude_workspaces

    main = tmp_path / "managed"
    main.mkdir()
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run([*git, "-C", str(main), "init", "-q"], check=True)
    subprocess.run(
        [*git, "-C", str(main), "commit", "-q", "--allow-empty", "-m", "i"],
        check=True,
    )
    worktree = tmp_path / "worktree"
    subprocess.run(
        [*git, "-C", str(main), "worktree", "add", "-q", str(worktree)],
        check=True,
    )
    trust_claude_workspaces([str(worktree)])
    projects = _claude_config(isolated_claude_config)["projects"]
    for path in (worktree, main):
        assert projects[str(path.resolve())]["hasTrustDialogAccepted"] is True


def test_unreadable_claude_config_blocks_launch(
    tmp_path, isolated_claude_config
):
    (isolated_claude_config / ".claude.json").write_text("{not json")
    with pytest.raises(HarnessError) as error:
        launch(tmp_path, trust_registered_repositories=True)
    assert error.value.failure["code"] == "harness.trust_failed"
    assert (isolated_claude_config / ".claude.json").read_text() == "{not json"
