"""Setup and watch controls without calling a remote GitHub repository."""

import json
import subprocess

import pytest

from pluto_factory.cli import main, parser
from pluto_factory.cli_github import github_slug, origin_slug, readiness
from pluto_factory.config import load_config
from pluto_factory.demo import create_repository
from pluto_factory.models import FactoryConfig, Repository


@pytest.fixture(autouse=True)
def isolated_configuration(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))


def setup(home, repo):
    command = json.dumps(["python3", "-m", "unittest"])
    return [
        "setup",
        "--no-input",
        "--home",
        str(home),
        "--name",
        "github-example",
        "--repo",
        f"app={repo}",
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
        "--integration",
        "github",
    ]


@pytest.mark.parametrize(
    "remote",
    [
        "git@github.com:example/snake.git",
        "https://github.com/example/snake.git",
        "ssh://git@github.com/example/snake.git",
    ],
)
def test_origin_slug_is_inferred_locally(tmp_path, monkeypatch, remote):
    observed = []

    def run(argv, **kwargs):
        observed.append(argv)
        return subprocess.CompletedProcess(argv, 0, remote, "")

    monkeypatch.setattr(subprocess, "run", run)
    assert origin_slug(tmp_path) == "example/snake"
    assert observed == [
        [
            "git",
            "-C",
            str(tmp_path),
            "remote",
            "get-url",
            "origin",
        ]
    ]


@pytest.mark.parametrize(
    "value",
    [
        "example/snake;echo",
        "../snake",
        "example/a/b",
        "https://github.com/a/b",
    ],
)
def test_github_slug_rejects_non_slug_values(value):
    with pytest.raises(ValueError, match="OWNER/REPOSITORY"):
        github_slug(value)


def test_setup_infers_origin_without_github_network_call(
    tmp_path, capsys, monkeypatch
):
    repo = create_repository(tmp_path / "repo")
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "remote",
            "add",
            "origin",
            "git@github.com:example/snake.git",
        ],
        check=True,
    )
    real_run = subprocess.run
    observed = []

    def run(argv, **kwargs):
        observed.append(argv)
        assert argv[0] != "gh", "setup must not call GitHub"
        return real_run(argv, **kwargs)

    monkeypatch.setattr(subprocess, "run", run)
    home = tmp_path / "factory"
    assert main(setup(home, repo)) == 0
    config = load_config(home)
    assert config.integration == "github"
    assert config.repositories[0].github == "example/snake"
    assert config.github_auto_merge is False
    result = json.loads(capsys.readouterr().out)
    assert result["readiness"]["github"]["authentication"] == "not_verified"


def test_missing_or_unregistered_github_mapping_preserves_files(
    tmp_path, capsys
):
    repo = create_repository(tmp_path / "repo")
    home = tmp_path / "factory"
    assert main(setup(home, repo)) == 1
    assert not home.exists()
    assert "requires a slug" in capsys.readouterr().err
    assert (
        main(setup(home, repo) + ["--github-repo", "other=example/snake"]) == 1
    )
    assert not home.exists()
    assert "unregistered" in capsys.readouterr().err


def test_github_master_defaults_and_explicit_boolean_overrides(
    tmp_path, capsys
):
    master = tmp_path / "xdg" / "pluto" / "pluto.toml"
    master.parent.mkdir(parents=True)
    master.write_text(
        '[factory]\nintegration = "github"\n'
        'github_issue_label = "queued-for-factory"\ngithub_poll_seconds = 11\n'
        "github_auto_merge = true\nclose_source_issues = false\n"
    )
    home = tmp_path / "factory"
    arguments = setup(home, create_repository(tmp_path / "repo"))
    arguments += [
        "--github-repo",
        "app=example/snake",
        "--no-auto-merge",
        "--close-source-issues",
    ]
    assert main(arguments) == 0
    config = load_config(home)
    assert config.github_issue_label == "queued-for-factory"
    assert config.github_poll_seconds == 11
    assert config.github_auto_merge is False
    assert config.close_source_issues is True
    assert json.loads(capsys.readouterr().out)["created"] is True


def test_doctor_auth_status_is_explicit_and_never_returns_backend_output(
    tmp_path, monkeypatch
):
    config = FactoryConfig(
        name="example",
        integration="github",
        repositories=[
            Repository(name="app", path=str(tmp_path), github="example/snake")
        ],
    )
    monkeypatch.setattr("shutil.which", lambda _: "/fixture/gh")
    observed = []

    def run(argv, **kwargs):
        observed.append(argv)
        return subprocess.CompletedProcess(
            argv, 0, "private-output", "private-output"
        )

    monkeypatch.setattr(subprocess, "run", run)
    _, status = readiness(config)
    assert status["authentication"] == "not_verified"
    assert not observed
    checks, status = readiness(config, verify_auth=True)
    assert status["authentication"] == "verified"
    assert all(check["passed"] for check in checks)
    assert observed == [["gh", "auth", "status", "--hostname", "github.com"]]
    assert "private-output" not in json.dumps(status)


def test_watch_is_tristate_for_engine_default():
    assert parser().parse_args(["start"]).watch is None
    assert parser().parse_args(["start", "--watch"]).watch is True
    assert parser().parse_args(["start", "--no-watch"]).watch is False
