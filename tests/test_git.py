"""Real disposable repositories exercise isolation and integration recovery."""

import subprocess
import sys
from pathlib import Path

import pytest

from pluto_factory.git import GitFailure, GitWorkspace
from pluto_factory.models import FactoryConfig, Repository


def git(path: Path, *arguments: str) -> str:
    result = subprocess.run(
        [
            "git",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "user.name=Factory Test",
            "-c",
            "user.email=test@localhost",
            "-c",
            "commit.gpgsign=false",
            *arguments,
        ],
        cwd=path,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def repository(tmp_path: Path, name: str = "source") -> Path:
    source = tmp_path / name
    source.mkdir()
    git(source, "init", "-b", "main")
    (source / "value.txt").write_text("old\n")
    (source / ".gitignore").write_text("__pycache__/\n")
    git(source, "add", ".")
    git(source, "commit", "-m", "Initial")
    return source


def workspace(tmp_path: Path, **kwargs) -> tuple[GitWorkspace, Path]:
    source = repository(tmp_path)
    config = FactoryConfig(
        name="test",
        repositories=[
            Repository(
                name="source",
                path=str(source),
                checks=[[sys.executable, "-c", "print('checked')"]],
                qa=[[sys.executable, "-c", "print('qa')"]],
            )
        ],
        **kwargs,
    )
    return GitWorkspace(tmp_path / "factory", config), source


def candidate(workspace: GitWorkspace, work_id: str = "item") -> list[dict]:
    entries = workspace.prepare(work_id, ["source"])
    (Path(entries[0]["worktree"]) / "value.txt").write_text("fixed\n")
    return workspace.snapshot(entries, work_id)


def test_owned_worktrees_and_integration_leave_source_unchanged(tmp_path):
    manager, source = workspace(tmp_path)
    original = git(source, "rev-parse", "HEAD")
    entries = candidate(manager)
    assert (
        manager.prepare("item", ["source"])[0]["candidate"]
        == (entries[0]["candidate"])
    )
    assert Path(entries[0]["review_artifact"]).read_text().find("+fixed") >= 0
    evidence = manager.validate(entries, "checks")
    assert evidence[0]["passed"]
    assert evidence[0]["revision"] == entries[0]["candidate"]
    merged = manager.integrate(entries)
    assert merged[0]["merged_revision"] == entries[0]["candidate"]
    assert git(source, "rev-parse", "HEAD") == original
    assert (source / "value.txt").read_text() == "old\n"
    assert git(source, "status", "--porcelain") == ""
    assert (
        manager.integrate(entries)[0]["merged_revision"]
        == (merged[0]["merged_revision"])
    )


def test_validation_runs_each_flag_state_in_candidate_checkout(tmp_path):
    manager, _ = workspace(tmp_path)
    manager.config.repositories[0].flag_environments = [
        {"FEATURE_EXPORT": "0"},
        {"FEATURE_EXPORT": "1"},
    ]
    manager.config.repositories[0].checks = [
        [
            sys.executable,
            "-c",
            (
                "import os,pathlib; "
                "assert pathlib.Path('value.txt').read_text() == 'fixed\\n'; "
                "assert pathlib.Path(os.environ['FACTORY_REPO_SOURCE']).exists(); "
                "print(os.environ['FEATURE_EXPORT'])"
            ),
        ]
    ]
    entries = candidate(manager)
    evidence = manager.validate(entries, "checks")
    assert len(evidence) == 2
    assert all(row["passed"] for row in evidence)
    assert {row["stdout"].strip() for row in evidence} == {"0", "1"}
    assert all(
        row["validation_root"] != entries[0]["worktree"] for row in evidence
    )
    assert all(Path(row["artifact"]).is_file() for row in evidence)


def test_nonzero_exit_is_failed_evidence(tmp_path):
    manager, _ = workspace(tmp_path)
    manager.config.repositories[0].checks = [
        [
            sys.executable,
            "-c",
            "import sys; print('failed'); sys.exit(7)",
        ]
    ]
    evidence = manager.validate(candidate(manager), "checks")
    assert not evidence[0]["passed"]
    assert evidence[0]["exit_code"] == 7
    assert "failed" in evidence[0]["stdout"]


@pytest.mark.parametrize("kind", ["checks", "qa"])
def test_missing_required_commands_cannot_pass(tmp_path, kind):
    manager, _ = workspace(tmp_path)
    setattr(manager.config.repositories[0], kind, [])
    with pytest.raises(GitFailure) as error:
        manager.validate(candidate(manager), kind)
    assert error.value.code == "validation.unconfigured"


def test_timeout_is_failed_evidence(tmp_path):
    manager, _ = workspace(tmp_path, command_timeout_seconds=1)
    manager.config.repositories[0].checks = [
        [
            sys.executable,
            "-c",
            "import time; time.sleep(30)",
        ]
    ]
    evidence = manager.validate(candidate(manager), "checks")
    assert not evidence[0]["passed"]
    assert evidence[0]["timed_out"]
    assert evidence[0]["exit_code"] == 124


def test_checks_cannot_pass_by_mutating_tracked_candidate(tmp_path):
    manager, _ = workspace(tmp_path)
    manager.config.repositories[0].checks = [
        [
            sys.executable,
            "-c",
            "from pathlib import Path; Path('value.txt').write_text('rewritten')",
        ]
    ]
    entries = candidate(manager)
    evidence = manager.validate(entries, "checks")
    assert not evidence[0]["passed"]
    assert evidence[0]["mutated_repositories"] == ["source"]
    assert (Path(entries[0]["worktree"]) / "value.txt").read_text() == "fixed\n"


def test_default_movement_requires_revalidation_and_sync(tmp_path):
    manager, _ = workspace(tmp_path)
    entries = candidate(manager)
    managed = Path(entries[0]["managed"])
    (managed / "another.txt").write_text("new default\n")
    git(managed, "add", ".")
    git(managed, "commit", "-m", "Other work")
    with pytest.raises(GitFailure) as error:
        manager.integrate(entries)
    assert error.value.code == "git.base_changed"
    updated, changed = manager.sync(entries)
    assert changed
    assert updated[0]["candidate"] != entries[0]["candidate"]
    assert all(row["passed"] for row in manager.validate(updated, "checks"))
    manager.integrate(updated)
    assert (managed / "value.txt").read_text() == "fixed\n"
    assert (managed / "another.txt").read_text() == "new default\n"


def test_conflict_preserves_worktree_for_repair(tmp_path):
    manager, source = workspace(tmp_path)
    entries = candidate(manager)
    managed = Path(entries[0]["managed"])
    (managed / "value.txt").write_text("other fix\n")
    git(managed, "add", ".")
    git(managed, "commit", "-m", "Conflicting work")
    with pytest.raises(GitFailure) as error:
        manager.sync(entries)
    assert error.value.code == "git.conflict"
    worktree = Path(entries[0]["worktree"])
    assert git(worktree, "ls-files", "--unmerged")
    assert "fixed" in (worktree / "value.txt").read_text()
    assert (source / "value.txt").read_text() == "old\n"


def test_partial_multirepository_integration_is_reconciled(
    tmp_path, monkeypatch
):
    first = repository(tmp_path, "first")
    second = repository(tmp_path, "second")
    config = FactoryConfig(
        name="test",
        repositories=[
            Repository(name="first", path=str(first)),
            Repository(name="second", path=str(second)),
        ],
    )
    manager = GitWorkspace(tmp_path / "factory", config)
    entries = manager.prepare("cross-repo", ["first", "second"])
    for entry in entries:
        (Path(entry["worktree"]) / "value.txt").write_text("fixed\n")
    entries = manager.snapshot(entries, "cross-repo")
    execute = manager._git

    def fail_second(cwd, *args, **kwargs):
        if args and args[0] == "merge" and cwd == Path(entries[1]["managed"]):
            raise GitFailure(
                "git.failed", "Injected second integration failure"
            )
        return execute(cwd, *args, **kwargs)

    monkeypatch.setattr(manager, "_git", fail_second)
    with pytest.raises(GitFailure):
        manager.integrate(entries)
    assert (
        git(Path(entries[0]["managed"]), "rev-parse", "HEAD")
        == (entries[0]["candidate"])
    )
    assert (
        git(Path(entries[1]["managed"]), "rev-parse", "HEAD")
        == (entries[1]["base"])
    )
    monkeypatch.setattr(manager, "_git", execute)
    merged = manager.integrate(entries)
    assert all(
        entry["merged_revision"] == entry["candidate"] for entry in merged
    )
    assert (first / "value.txt").read_text() == "old\n"
    assert (second / "value.txt").read_text() == "old\n"


def test_readonly_repository_changes_are_rejected(tmp_path):
    source = repository(tmp_path)
    manager = GitWorkspace(
        tmp_path / "factory",
        FactoryConfig(
            name="test",
            repositories=[
                Repository(name="source", path=str(source), access="read")
            ],
        ),
    )
    entries = manager.prepare("item", ["source"])
    (Path(entries[0]["worktree"]) / "value.txt").write_text("unauthorized\n")
    with pytest.raises(GitFailure) as error:
        manager.snapshot(entries, "item")
    assert error.value.code == "scope.denied"


def test_unregistered_or_forged_paths_are_rejected(tmp_path):
    manager, source = workspace(tmp_path)
    with pytest.raises(GitFailure) as error:
        manager.prepare("item", ["elsewhere"])
    assert error.value.code == "scope.denied"
    entries = manager.prepare("item", ["source"])
    entries[0]["worktree"] = str(source)
    with pytest.raises(GitFailure) as error:
        manager.snapshot(entries, "item")
    assert error.value.code == "scope.denied"


def test_post_snapshot_changes_invalidate_candidate(tmp_path):
    manager, _ = workspace(tmp_path)
    entries = candidate(manager)
    (Path(entries[0]["worktree"]) / "value.txt").write_text("late change\n")
    with pytest.raises(GitFailure) as error:
        manager.validate(entries, "checks")
    assert error.value.code == "git.candidate_changed"


def test_snapshot_cannot_be_reassigned_to_another_item(tmp_path):
    manager, _ = workspace(tmp_path)
    entries = manager.prepare("original-item", ["source"])
    with pytest.raises(GitFailure) as error:
        manager.snapshot(entries, "different-item")
    assert error.value.code == "scope.denied"


def test_empty_increment_is_valid_and_default_branch_is_detected(tmp_path):
    manager, source = workspace(tmp_path)
    git(source, "checkout", "-b", "unrelated-feature")
    entries = manager.prepare("item", ["source"])
    assert entries[0]["default_branch"] == "main"
    entries = manager.snapshot(entries, "item")
    assert entries[0]["candidate"] == entries[0]["base"]
    assert manager.validate(entries, "qa")[0]["passed"]


def test_cleanup_refuses_unintegrated_or_dirty_work(tmp_path):
    manager, _ = workspace(tmp_path)
    entries = candidate(manager)
    with pytest.raises(GitFailure) as error:
        manager.cleanup(entries)
    assert error.value.code == "git.not_integrated"
    manager.integrate(entries)
    (Path(entries[0]["worktree"]) / "valuable.txt").write_text("keep this\n")
    with pytest.raises(GitFailure) as error:
        manager.cleanup(entries)
    assert error.value.code == "git.dirty_worktree"


@pytest.mark.parametrize("coupled", [False, True])
def test_integration_plan_validates_mixed_candidate_prefixes(tmp_path, coupled):
    provider = repository(tmp_path, "provider")
    consumer = repository(tmp_path, "consumer")
    independent_check = [
        sys.executable,
        "-c",
        (
            "from pathlib import Path; "
            "assert Path('value.txt').read_text() in ('old\\n', 'new\\n')"
        ),
    ]
    coupled_check = [
        sys.executable,
        "-c",
        (
            "import os; from pathlib import Path; "
            "provider = Path(os.environ['FACTORY_REPO_PROVIDER']); "
            "assert (provider / 'value.txt').read_text() == "
            "Path('value.txt').read_text()"
        ),
    ]
    consumer_check = coupled_check if coupled else independent_check
    config = FactoryConfig(
        name="test",
        repositories=[
            Repository(
                name="provider",
                path=str(provider),
                checks=[independent_check],
                qa=[independent_check],
            ),
            Repository(
                name="consumer",
                path=str(consumer),
                checks=[consumer_check],
                qa=[consumer_check],
            ),
        ],
    )
    manager = GitWorkspace(tmp_path / "factory", config)
    entries = manager.prepare("cross-repo", ["provider", "consumer"])
    for entry in entries:
        (Path(entry["worktree"]) / "value.txt").write_text("new\n")
    entries = manager.snapshot(entries, "cross-repo")
    assert all(row["passed"] for row in manager.validate(entries, "checks"))
    evidence = manager.validate_integration_plan(entries)
    assert len(evidence) == 4
    assert {row["kind"] for row in evidence} == {"compatibility"}
    assert {row["profile"] for row in evidence} == {"checks", "qa"}
    for row in evidence:
        assert row["integration_order"] == ["provider", "consumer"]
        assert row["integration_prefix"] == ["provider"]
        assert row["candidate_set"] == {
            "provider": entries[0]["candidate"],
            "consumer": entries[1]["base"],
        }
    if coupled:
        failures = [row for row in evidence if not row["passed"]]
        assert len(failures) == 2
        assert all(row["repository"] == "consumer" for row in failures)
    else:
        assert all(row["passed"] for row in evidence)
    assert all(
        git(Path(entry["managed"]), "rev-parse", "HEAD") == entry["base"]
        for entry in entries
    )
