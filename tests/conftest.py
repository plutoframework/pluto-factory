"""Shared test isolation."""

import pytest


@pytest.fixture(autouse=True)
def isolated_claude_config(tmp_path_factory, monkeypatch):
    """Never let tests read or write the developer's real Claude config."""
    folder = tmp_path_factory.mktemp("claude-config")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(folder))
    return folder
