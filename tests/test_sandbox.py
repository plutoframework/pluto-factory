"""nono sandbox wrapping for workers, without launching an agent."""

import shutil
import subprocess
from pathlib import Path

import pytest
from test_claude_worker import ClaudeCore
from test_harness import FakeCore, assignment, terminal

from pluto_factory import sandbox
from pluto_factory.harness import ClaudeHarness, HarnessError, PiHarness
from pluto_factory.models import FactoryConfig, Repository, SandboxPolicy


@pytest.fixture
def ready(monkeypatch, tmp_path):
    binary = tmp_path / "tools" / "agent" / "1.0" / "agent"
    binary.parent.mkdir(parents=True)
    binary.write_text("")
    monkeypatch.setattr(sandbox, "check", lambda _policy: None)
    monkeypatch.setattr(sandbox, "resolve_binary", lambda _name: binary)
    monkeypatch.setenv("HOME", str(tmp_path / "user"))
    monkeypatch.delenv("PI_CODING_AGENT_DIR", raising=False)
    return binary


def config(tmp_path, harness, **policy):
    return FactoryConfig(
        name="test",
        repositories=[Repository(name="sample", path=str(tmp_path))],
        harness=harness,
        isolation="required",
        sandbox=SandboxPolicy(**policy),
    )


def git_worktree(tmp_path):
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
    return main.resolve(), worktree.resolve()


def flags(command, flag):
    return [command[i + 1] for i, value in enumerate(command) if value == flag]


def test_pi_launch_runs_inside_nono_with_scoped_grants(tmp_path, ready):
    main, worktree = git_worktree(tmp_path)
    item = assignment(tmp_path)
    item["directory"] = str(worktree)
    item["roots"] = [
        {"name": "sample", "path": str(worktree), "access": "write"},
        {"name": "docs", "path": str(main), "access": "read"},
    ]
    core = FakeCore()
    extra = tmp_path / "toolchain"
    extra.mkdir()
    session = PiHarness(
        config(tmp_path, "pi", read=[str(extra)], allow_domains=["x.dev"]),
        core,
    ).launch(item)
    call = next(call for call in core.calls if call[1] == "run")
    assert "sandbox=nono" in call
    assert session["sandboxed"] is True
    # Credentials are injected outside; the launcher then execs nono.
    launcher = call.index("pluto_factory.worker_launcher")
    command = call[launcher + 2 :]
    assert command[0] == "env"
    folder = str(Path(item["assignment_path"]).parent)
    assert command[1] == f"TMPDIR={folder}/tmp"
    assert command[2] == "GIT_CONFIG_GLOBAL=/dev/null"
    assert command[3:5] == ["nono", "run"]
    assert flags(command, "--profile") == [str(sandbox.PROFILE)]
    native = command[command.index("--") + 1 :]
    assert native[0] == str(ready)
    assert native[-1] == f"@{item['packet_path']}"
    write = set(flags(command, "--allow"))
    read = set(flags(command, "--read"))
    assert {
        str(worktree),
        folder,
        f"{folder}/tmp",
        item["session_dir"],
    } <= write
    # Worktree admin folder is writable; shared git objects are read-only.
    assert str(main / ".git" / "worktrees" / "worktree") in write
    assert str(main / ".git") in read
    assert {str(main), str(extra), str(ready.parent.parent)} <= read
    assert str(tmp_path / "user" / ".pi" / "agent") in write
    assert flags(command, "--allow-domain") == ["x.dev"]
    assert not write & read


def test_claude_sandbox_grants_its_config(
    tmp_path, ready, isolated_claude_config
):
    (isolated_claude_config / ".claude.json").write_text("{}")
    item = assignment(tmp_path)
    core = ClaudeCore()
    ClaudeHarness(config(tmp_path, "claude"), core).launch(item)
    call = next(call for call in core.calls if call[1] == "run")
    command = call[call.index("nono") :]
    assert str(isolated_claude_config) in flags(command, "--allow")
    assert flags(command, "--allow-file") == [
        str(isolated_claude_config / ".claude.json")
    ]
    native = command[command.index("--") + 1 :]
    assert native[0] == str(ready)
    assert native[native.index("--permission-mode") + 1] == "dontAsk"


def test_sandboxed_pi_session_never_rebinds(tmp_path, ready):
    item = assignment(tmp_path)
    core = FakeCore([terminal(item)])
    harness = PiHarness(config(tmp_path, "pi"), core)
    session = harness.launch(item)
    with pytest.raises(HarnessError) as caught:
        harness.reassign(session, {**item, "generation": 2})
    assert caught.value.failure["code"] == "config.fresh_session_required"


@pytest.mark.skipif(not shutil.which("nono"), reason="nono not installed")
def test_bundled_profile_blocks_herdr_socket():
    assert sandbox.check({}) is None


def test_missing_nono_is_reported(monkeypatch):
    monkeypatch.setattr(sandbox.shutil, "which", lambda _name: None)
    assert "not installed" in sandbox.check({})


def test_policy_validation_rejects_dangerous_values(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "user"))
    monkeypatch.setenv("HC_TERMINAL_SOCKET", str(tmp_path / "herdr.sock"))
    (tmp_path / "user").mkdir()
    home = tmp_path / "factory"
    home.mkdir()
    for bad in (
        "/",
        str(tmp_path),  # ancestor of $HOME and the factory home
        str(tmp_path / "user"),
        str(home),
        str(home / "sessions"),
        str(tmp_path / "user" / ".ssh"),
        "/var/run/docker.sock",
        str(tmp_path / "herdr.sock"),
    ):
        with pytest.raises(ValueError):
            sandbox.validate_policy(SandboxPolicy(read_paths=[bad]), home)
    sandbox.validate_policy(
        SandboxPolicy(write_paths=[str(tmp_path / "user" / "go")]), home
    )


def test_model_rejects_malformed_values():
    for bad in (
        {"read_paths": ["relative/path"]},
        {"write_paths": ["/a/../etc"]},
        {"allow_domains": ["bad domain"]},
        {"env_passthrough": ["1BAD"]},
        {"env_passthrough": ["DOCKER_HOST"]},
        {"env_passthrough": ["HC_TERMINAL_SOCKET"]},
    ):
        with pytest.raises(ValueError):
            SandboxPolicy(**bad)
    assert SandboxPolicy(read=["/opt/x"]).read_paths == ["/opt/x"]


def test_env_passthrough_and_docker_opt_in(
    tmp_path, ready, monkeypatch, caplog
):
    monkeypatch.setenv("GOFLAGS", "-mod=mod")
    monkeypatch.delenv("MISSING_VAR", raising=False)
    cfg = config(tmp_path, "pi", env_passthrough=["GOFLAGS", "MISSING_VAR"])
    command = sandbox.wrap(
        assignment(tmp_path), "pi", cfg.sandbox.model_dump(), ["pi"]
    )
    assert "GOFLAGS=-mod=mod" in command
    assert not any(c.startswith("MISSING_VAR") for c in command)
    assert "--allow-unix-socket" not in command
    opened = config(tmp_path, "pi", allow_docker=True)
    with caplog.at_level("WARNING"):
        command = sandbox.wrap(
            assignment(tmp_path),
            "pi",
            opened.sandbox.model_dump(),
            ["pi"],
        )
    assert flags(command, "--allow-unix-socket") == [sandbox.DOCKER_SOCKET]
    assert "root-equivalent" in caplog.text


def test_effective_policy_and_digest(tmp_path):
    shown = sandbox.effective(SandboxPolicy(allow_docker=True).model_dump())
    assert "DOCKER SOCKET EXPOSED" in shown["host_sockets"]
    assert sandbox.effective({})["network"] == "open"
    from pluto_factory.config import digest

    base = config(tmp_path, "pi")
    other = config(tmp_path, "pi", allow_docker=True)
    assert digest(base.model_dump()) != digest(other.model_dump())
