"""Public CLI behavior, including headless safety and wizard cancellation."""

import io
import json
import subprocess
from pathlib import Path

import pytest

from pluto_factory.cli import main, parser
from pluto_factory.config import load_config
from pluto_factory.credentials import credential_environment


@pytest.fixture(autouse=True)
def isolated_master_configuration(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))


def repository(path: Path) -> Path:
    path.mkdir()
    subprocess.run(
        ["git", "init", "-b", "main", str(path)],
        check=True,
        capture_output=True,
    )
    (path / "hello.py").write_text("print('hello')\n")
    subprocess.run(["git", "-C", str(path), "add", "."], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(path),
            "-c",
            "user.name=CLI Test",
            "-c",
            "user.email=cli@example.invalid",
            "commit",
            "-m",
            "initial",
        ],
        check=True,
        capture_output=True,
    )
    return path


def setup_args(home: Path, repo: Path) -> list[str]:
    command = json.dumps(["python3", "-m", "unittest"])
    return [
        "setup",
        "--home",
        str(home),
        "--name",
        "example",
        "--repo",
        f"application={repo}",
        "--check",
        command,
        "--qa",
        command,
        "--harness",
        "fixture",
        "--coordinator",
        "rules",
        "--isolation",
        "cooperative",
        "--no-input",
    ]


def test_setup_scaffolds_headless_factory(tmp_path, capsys):
    home = tmp_path / "factory"
    repo = repository(tmp_path / "repo")
    assert main(setup_args(home, repo)) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["created"] is True
    assert result["readiness"]["ready"] is True
    config = load_config(home)
    assert config.repositories[0].path == str(repo.resolve())
    assert config.worker.model == "gpt-6.1-sol"
    for role in ("orchestrator", "implementer", "reviewer", "qa"):
        assert (home / "prompts" / f"{role}.md").read_text()
    assert (home / "state.sqlite3").exists()


def test_setup_existing_factory_preserves_authored_prompt(tmp_path, capsys):
    home = tmp_path / "factory"
    repo = repository(tmp_path / "repo")
    args = setup_args(home, repo)
    assert main(args) == 0
    prompt = home / "prompts" / "implementer.md"
    prompt.write_text("authored guidance")
    assert main(args) == 1
    assert prompt.read_text() == "authored guidance"
    assert "already exists" in capsys.readouterr().err


def test_setup_piped_input_never_prompts_or_writes(
    tmp_path, monkeypatch, capsys
):
    home = tmp_path / "factory"
    monkeypatch.setattr("sys.stdin", io.StringIO("should not be consumed"))
    assert main(["setup", "--home", str(home)]) == 1
    assert not home.exists()
    assert "noninteractive" in capsys.readouterr().err


def test_wizard_cancellation_has_no_side_effects(tmp_path, monkeypatch, capsys):
    from pluto_factory import cli

    home = tmp_path / "factory"
    repo = repository(tmp_path / "repo")

    def fake_wizard(args):
        args.name = "example"
        args.repo = [f"app={repo}"]
        args.isolation = "cooperative"
        return True

    monkeypatch.setattr(cli, "_wizard", fake_wizard)
    monkeypatch.setattr("builtins.input", lambda _: "n")
    assert main(["setup", "--home", str(home)]) == 0
    assert not home.exists()
    output = capsys.readouterr().out
    assert '"reason": "cancelled"' in output


def test_global_options_work_on_either_side_of_subcommand(tmp_path):
    path = str(tmp_path / "factory")
    before = parser().parse_args(["--home", path, "--no-input", "inspect"])
    after = parser().parse_args(["inspect", "--home", path, "--no-input"])
    assert before.home == after.home
    assert before.no_input == after.no_input is True


def test_init_alias(tmp_path, capsys):
    args = setup_args(tmp_path / "factory", repository(tmp_path / "repo"))
    args[0] = "init"
    assert main(args) == 0
    assert json.loads(capsys.readouterr().out)["created"] is True


def test_command_arguments_are_json_not_shell(tmp_path, capsys):
    args = setup_args(tmp_path / "factory", repository(tmp_path / "repo"))
    args[args.index("--check") + 1] = "python3 -m unittest; touch nope"
    assert main(args) == 1
    assert not (tmp_path / "factory").exists()
    assert capsys.readouterr().err


def test_doctor_reports_required_isolation_unavailable(tmp_path, capsys):
    args = setup_args(tmp_path / "factory", repository(tmp_path / "repo"))
    args[args.index("--isolation") + 1] = "required"
    assert main(args) == 0
    capsys.readouterr()
    assert main(["doctor", "--home", str(tmp_path / "factory")]) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["ready"] is False
    assert any(
        check["name"] == "execution-isolation" and not check["passed"]
        for check in result["checks"]
    )


def test_scope_root_must_be_explicit(tmp_path, capsys):
    repo = repository(tmp_path / "repo")
    child = repo / "child"
    child.mkdir()
    home = tmp_path / "factory"
    assert main(setup_args(home, child)) == 1
    assert not home.exists()
    assert "repository root" in capsys.readouterr().err


def test_api_key_is_never_echoed(tmp_path, monkeypatch, capsys):
    secret = "test-only-sensitive-value"
    monkeypatch.setenv("FACTORY_TEST_KEY", secret)
    home = tmp_path / "factory"
    args = setup_args(home, repository(tmp_path / "repo")) + [
        "--auth",
        "api_key",
        "--api-key-env",
        "FACTORY_TEST_KEY",
    ]
    assert main(args) == 0
    output = capsys.readouterr()
    assert secret not in output.out + output.err
    assert secret not in (home / "factory.toml").read_text()


def test_api_key_stdin_is_stored_privately(tmp_path, monkeypatch, capsys):
    import shutil

    if not shutil.which("age-keygen") or not shutil.which("fnox"):
        pytest.skip("real age/fnox executables are required")
    secret = "test-only-private-token"
    monkeypatch.setattr("sys.stdin", io.StringIO(secret + "\n"))
    home = tmp_path / "factory"
    args = setup_args(home, repository(tmp_path / "repo")) + [
        "--auth",
        "api_key",
        "--api-key-env",
        "FACTORY_STDIN_KEY",
        "--api-key-stdin",
        "--generate-age-key",
    ]
    assert main(args) == 0
    output = capsys.readouterr()
    stored = home / "fnox.toml"
    assert stored.stat().st_mode & 0o077 == 0
    assert secret not in stored.read_text()
    assert not (home / "credentials.json").exists()
    assert credential_environment(home) == {"FACTORY_STDIN_KEY": secret}
    assert secret not in output.out + output.err
    assert secret not in (home / "factory.toml").read_text()
    config = load_config(home)
    assert config.worker.provider == "openai"
    assert config.reviewer.provider == "openai"
    assert config.qa_model.provider == "openai"
    assert config.coordinator_model == "openai:gpt-6.1-terra"


def test_master_defaults_are_frozen_and_explicit_flags_win(tmp_path, capsys):
    master = tmp_path / "xdg" / "pluto" / "pluto.toml"
    master.parent.mkdir(parents=True)
    master.write_text(
        "[factory]\nmax_recoveries = 7\n"
        '[factory.worker]\nmodel = "global-worker"\n'
        '[factory.secrets]\nprovider = "1password"\nop_vault = "Engineering"\n'
    )
    first = tmp_path / "first"
    repo = repository(tmp_path / "repo")
    assert main(setup_args(first, repo)) == 0
    capsys.readouterr()
    config = load_config(first)
    assert config.max_recoveries == 7
    assert config.worker.model == "global-worker"
    assert 'type = "1password"' in (first / "fnox.toml").read_text()
    second = tmp_path / "second"
    assert (
        main(
            setup_args(second, repo)
            + [
                "--model",
                "explicit-worker",
                "--max-recoveries",
                "2",
                "--secrets-provider",
                "age",
            ]
        )
        == 0
    )
    assert load_config(second).worker.model == "explicit-worker"
    assert load_config(second).max_recoveries == 2
    master.write_text('[factory.worker]\nmodel = "changed-global"\n')
    assert load_config(first).worker.model == "global-worker"


def test_invalid_master_is_rejected_before_writes(tmp_path, capsys):
    master = tmp_path / "xdg" / "pluto" / "pluto.toml"
    master.parent.mkdir(parents=True)
    master.write_text('[factory.secrets]\nprovider = "plaintext"\n')
    home = tmp_path / "factory"
    assert main(setup_args(home, repository(tmp_path / "repo"))) == 1
    assert not home.exists()
    assert (
        "master factory secret defaults are invalid" in capsys.readouterr().err
    )


def test_factory_config_overrides_master_and_cli_overrides_both(
    tmp_path, capsys
):
    master = tmp_path / "xdg" / "pluto" / "pluto.toml"
    master.parent.mkdir(parents=True)
    master.write_text(
        "[factory]\nmax_recoveries = 6\n"
        '[factory.worker]\nmodel = "global-worker"\nreasoning = "medium"\n'
    )
    repo = repository(tmp_path / "repo")
    source = tmp_path / "template.toml"
    source.write_text(
        'name = "from-file"\nharness = "fixture"\ncoordinator = "rules"\n'
        'isolation = "cooperative"\nmax_recoveries = 4\n'
        '[worker]\nmodel = "configured-worker"\n'
        '[[repositories]]\nname = "app"\n'
        f"path = {json.dumps(str(repo))}\n"
        'checks = [["python3", "-m", "unittest"]]\n'
        'qa = [["python3", "-m", "unittest"]]\n'
    )
    home = tmp_path / "factory"
    assert (
        main(
            [
                "setup",
                "--home",
                str(home),
                "--config",
                str(source),
                "--no-input",
                "--model",
                "explicit-worker",
            ]
        )
        == 0
    )
    config = load_config(home)
    assert config.max_recoveries == 4
    assert config.worker.model == "explicit-worker"
    assert config.worker.reasoning == "medium"
    assert json.loads(capsys.readouterr().out)["created"] is True


def test_master_factory_file_is_distinct_from_project_manifest(
    tmp_path, capsys, monkeypatch
):
    project = tmp_path / "project"
    project.mkdir()
    (project / "pluto.toml").write_text("[factory]\nmax_recoveries = 19\n")
    monkeypatch.chdir(project)
    assert main(["defaults", "show"]) == 0
    defaults = json.loads(capsys.readouterr().out)
    assert defaults["factory"]["max_recoveries"] == 3
    assert defaults["secrets"]["provider"] == "age"
    assert defaults["exists"] is False


def test_ci_does_not_enter_wizard(tmp_path, monkeypatch, capsys):
    class Terminal(io.StringIO):
        def isatty(self):
            return True

    home = tmp_path / "factory"
    monkeypatch.setenv("CI", "true")
    monkeypatch.setattr("sys.stdin", Terminal())
    monkeypatch.setattr("sys.stdout", Terminal())
    monkeypatch.setattr(
        "builtins.input", lambda _: pytest.fail("prompted in CI")
    )
    assert main(["setup", "--home", str(home)]) == 1
    assert not home.exists()
    assert "noninteractive" in capsys.readouterr().err


def test_interactive_and_headless_cannot_be_combined(tmp_path, capsys):
    home = tmp_path / "factory"
    assert (
        main(
            [
                "--interactive",
                "setup",
                "--no-input",
                "--home",
                str(home),
            ]
        )
        == 1
    )
    assert not home.exists()
    assert "cannot be combined" in capsys.readouterr().err


@pytest.mark.parametrize("missing", ["--check", "--qa"])
def test_missing_validation_is_not_ready(tmp_path, missing, capsys):
    home = tmp_path / "factory"
    args = setup_args(home, repository(tmp_path / "repo"))
    index = args.index(missing)
    del args[index : index + 2]
    assert main(args) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["readiness"]["ready"] is False


def test_setup_claude_harness_uses_claude_model_defaults(tmp_path, capsys):
    home = tmp_path / "factory"
    repo = repository(tmp_path / "repo")
    args = setup_args(home, repo)
    args[args.index("fixture")] = "claude"
    main(args)
    capsys.readouterr()
    config = load_config(home)
    assert config.harness == "claude"
    assert config.worker.provider == "anthropic"
    assert config.worker.model == "claude-opus-5-5"
    assert config.reviewer.model == "claude-opus-5-5"
    assert config.qa_model.model == "claude-sonnet-5-5"
    assert config.qa_model.reasoning == "medium"


def test_setup_claude_harness_preserves_explicit_model(tmp_path, capsys):
    home = tmp_path / "factory"
    repo = repository(tmp_path / "repo")
    args = setup_args(home, repo)
    args[args.index("fixture")] = "claude"
    main([*args, "--model", "claude-fable-5-2"])
    capsys.readouterr()
    config = load_config(home)
    assert config.worker.model == "claude-fable-5-2"
    assert config.reviewer.model == "claude-opus-5-5"


def test_setup_claude_harness_uses_claude_coordinator(tmp_path, capsys):
    home = tmp_path / "factory"
    args = setup_args(home, repository(tmp_path / "repo"))
    args[args.index("fixture")] = "claude"
    main(args)
    capsys.readouterr()
    config = load_config(home)
    assert config.coordinator_runtime == "claude_agent_sdk"
    assert config.coordinator_model == "claude-sonnet-5-5"


def test_setup_claude_harness_preserves_explicit_coordinator(tmp_path, capsys):
    home = tmp_path / "factory"
    args = setup_args(home, repository(tmp_path / "repo"))
    args[args.index("fixture")] = "claude"
    main(
        [
            *args,
            "--coordinator-runtime",
            "pydantic_ai",
            "--coordinator-model",
            "openai:gpt-6.1-terra",
        ]
    )
    capsys.readouterr()
    config = load_config(home)
    assert config.coordinator_runtime == "pydantic_ai"
    assert config.coordinator_model == "openai:gpt-6.1-terra"


def test_setup_pi_harness_can_select_claude_coordinator(tmp_path, capsys):
    home = tmp_path / "factory"
    args = setup_args(home, repository(tmp_path / "repo"))
    main([*args, "--coordinator-runtime", "claude_agent_sdk"])
    capsys.readouterr()
    config = load_config(home)
    assert config.harness == "fixture"
    assert config.coordinator_runtime == "claude_agent_sdk"
    assert config.coordinator_model == "claude-sonnet-5-5"


def _scripted(monkeypatch, answers):
    replies = iter(answers)
    monkeypatch.setattr("builtins.input", lambda _: next(replies))


def _setup_namespace(monkeypatch, tmp_path):
    from pluto_factory.cli import _apply_master, parser

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    args = parser().parse_args(["setup"])
    args._explicit = set()
    _apply_master(args)
    return args


def test_choose_reprompts_until_valid(monkeypatch, capsys):
    from pluto_factory.cli import _choose

    _scripted(monkeypatch, ["bogus", "", "b"])
    assert _choose("Pick", ["a", "b"], "a") == "a"
    assert _choose("Pick", ["a", "b"], "a") == "b"
    assert "Choose one of" in capsys.readouterr().err


def test_ai_account_claude_aligns_everything(monkeypatch, tmp_path):
    from pluto_factory.cli import _ask_ai_account

    args = _setup_namespace(monkeypatch, tmp_path)
    _scripted(monkeypatch, ["claude"])
    _ask_ai_account(args)
    assert args.harness == "claude"
    assert args.coordinator_runtime == "claude_agent_sdk"
    assert args.coordinator_model == "claude-sonnet-5-5"
    assert args.provider == "anthropic"
    assert args.model == "claude-opus-5-5"


def test_ai_account_openai_reverts_claude_defaults(monkeypatch, tmp_path):
    from pluto_factory.cli import _ask_ai_account

    args = _setup_namespace(monkeypatch, tmp_path)
    _scripted(monkeypatch, ["claude", "openai"])
    _ask_ai_account(args)
    _ask_ai_account(args)
    assert args.harness == "pi"
    assert args.coordinator_runtime == "pydantic_ai"
    assert args.coordinator_model == "openai-codex:gpt-6.1-terra"
    assert args.provider == "openai"
    assert args.model == "gpt-6.1-sol"
    assert args.qa_model == "gpt-6.1-terra"


def test_claude_api_key_defaults_to_anthropic_env(monkeypatch, tmp_path):
    from pluto_factory.cli import _harness_model_defaults

    args = _setup_namespace(monkeypatch, tmp_path)
    args.harness = "claude"
    args.auth = "api_key"
    _harness_model_defaults(args)
    assert args.api_key_env == "ANTHROPIC_API_KEY"
