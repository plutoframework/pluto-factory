"""Disposable, deterministic work through the real coordinator and Git path."""

import json
import subprocess
import sys
from pathlib import Path

from .config import write_config
from .models import FactoryConfig, Repository, Submission

SOURCE = """import os


def add(left, right):
    return left - right


def greeting():
    if os.environ.get("PLUTO_DEMO_FEATURE") == "1":
        return "new greeting"
    return "stable greeting"
"""

TEST = """import os
import unittest

from calculator import add, greeting


class CalculatorTests(unittest.TestCase):
    def test_flag(self):
        expected = (
            "new greeting" if os.environ.get("PLUTO_DEMO_FEATURE") == "1"
            else "stable greeting"
        )
        self.assertEqual(greeting(), expected)
"""


def _git(path: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(path), *arguments],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(
            f"demo Git operation failed: {result.stderr.strip()}"
        )
    return result.stdout.strip()


def create_repository(path: Path) -> Path:
    path.mkdir(parents=True)
    (path / "calculator.py").write_text(SOURCE)
    (path / "test_calculator.py").write_text(TEST)
    (path / "factory_fixture.json").write_text(
        json.dumps(
            {
                "edits": [
                    {
                        "file": "calculator.py",
                        "replacements": [
                            {
                                "old": "return left - right",
                                "new": "return left + right",
                            }
                        ],
                    },
                    {
                        "file": "test_calculator.py",
                        "replacements": [
                            {
                                "old": "class CalculatorTests(unittest.TestCase):",
                                "new": (
                                    "class CalculatorTests(unittest.TestCase):\n"
                                    "    def test_addition(self):\n"
                                    "        self.assertEqual(add(2, 3), 5)\n"
                                ),
                            }
                        ],
                    },
                ],
            }
        )
    )
    _git(path, "init", "-b", "main")
    _git(path, "add", ".")
    _git(
        path,
        "-c",
        "user.name=Factory Demo",
        "-c",
        "user.email=factory-demo@example.invalid",
        "commit",
        "-m",
        "Create disposable example with an untested addition bug",
    )
    return path


def prepare_demo(directory: Path, scenario: str = "fix") -> tuple[Path, dict]:
    directory = directory.expanduser().resolve()
    if directory.exists() and any(directory.iterdir()):
        raise ValueError(
            "demo directory must be empty; existing files preserved"
        )
    directory.mkdir(parents=True, exist_ok=True)
    repos = [
        create_repository(directory / "sources" / name)
        for name in ("core", "cli")
    ]
    before = {repo.name: _git(repo, "rev-parse", "HEAD") for repo in repos}
    home = directory / "factory"
    config = FactoryConfig(
        name="demo",
        harness="fixture",
        coordinator="rules",
        isolation="cooperative",
        retry_base_seconds=0,
        repositories=[
            Repository(
                name=repo.name,
                path=str(repo),
                checks=[[sys.executable, "-m", "unittest", "-v"]],
                qa=[[sys.executable, "-m", "unittest", "-v"]],
                flag_environments=[
                    {"PLUTO_DEMO_FEATURE": "0"},
                    {"PLUTO_DEMO_FEATURE": "1"},
                ],
            )
            for repo in repos
        ],
    )
    write_config(home, config)
    from .state import State

    state = State(home)
    submitted = state.submit(
        Submission(
            title="Repair addition across the two example repositories",
            instruction=(
                "Fix add(left, right) in both repositories. Keep existing "
                "feature-flag behavior unchanged. The fixture recipe describes "
                "the expected edit."
            ),
            acceptance=[
                "add(2, 3) returns 5 in both repositories",
                "the existing feature works with its flag disabled and enabled",
            ],
            repositories=[repo.name for repo in repos],
            source="disposable-demo",
            delivery_key=f"demo-{scenario}",
            fixture_scenario=scenario,
        )
    )
    return home, {"submitted": submitted, "source_revisions": before}


def run_demo(
    directory: Path,
    scenario: str = "fix",
    max_seconds: float = 60,
) -> dict:
    from .engine import Engine
    from .state import State

    home, prepared = prepare_demo(directory, scenario)
    outcome = Engine(home).run(max_seconds=max_seconds)
    snapshot = State(home).inspect()
    sources = home.parent / "sources"
    unchanged = all(
        _git(sources / name, "rev-parse", "HEAD") == revision
        and "return left - right"
        in (sources / name / "calculator.py").read_text()
        for name, revision in prepared["source_revisions"].items()
    )
    return {
        "mode": "deterministic fixture; no model inference",
        "home": str(home),
        "scenario": scenario,
        "source_repositories_unchanged": unchanged,
        "submitted": prepared["submitted"],
        "run": outcome,
        "state": snapshot,
    }
