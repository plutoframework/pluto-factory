"""Portable QA must use the project's locked dependencies and fail faithfully."""

import importlib.util
import subprocess
from pathlib import Path


def helper():
    path = (
        Path(__file__).parents[1]
        / "src/pluto_factory/examples/snake/portable_qa.py"
    )
    spec = importlib.util.spec_from_file_location("portable_snake_qa", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_portable_qa_removes_host_overrides_and_keeps_feature_profile(
    monkeypatch,
):
    module = helper()
    for key in module.OVERRIDES:
        monkeypatch.setenv(key, "host-specific-value")
    monkeypatch.setenv("SNAKE_FEATURES", "all")
    calls = []

    def run(arguments, *, env, check):
        calls.append(arguments)
        assert not any(key in env for key in module.OVERRIDES)
        assert env["SNAKE_FEATURES"] == "all"
        assert check is False
        return subprocess.CompletedProcess(arguments, 0)

    assert module.run_portable_qa(run) == 0
    assert calls == [
        ["npm", "ci"],
        ["npx", "--no-install", "playwright", "install", "chromium"],
        ["npm", "run", "qa"],
    ]
    assert all(
        __import__("os").environ[key] == "host-specific-value"
        for key in module.OVERRIDES
    )


def test_install_failure_cannot_be_reported_as_passing_qa():
    module = helper()
    calls = []

    def run(arguments, **_options):
        calls.append(arguments)
        return subprocess.CompletedProcess(arguments, 23)

    assert module.run_portable_qa(run) == 23
    assert calls == [["npm", "ci"]]
