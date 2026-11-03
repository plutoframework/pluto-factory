"""Owned Git workspaces and evidence from immutable candidate checkouts.

The prototype integrates only into its managed clones. Original repositories
are read-only clone sources; publication is deliberately a separate adapter.
"""

import fcntl
import hashlib
import json
import os
import signal
import subprocess
import time
import uuid
from contextlib import ExitStack
from pathlib import Path

from .config import atomic_json
from .models import FactoryConfig, Repository


class GitFailure(RuntimeError):
    def __init__(
        self, code: str, summary: str, uncertain: bool = False
    ) -> None:
        super().__init__(summary)
        self.code = code
        self.summary = summary
        self.uncertain = uncertain


class GitWorkspace:
    def __init__(self, home: Path, config: FactoryConfig) -> None:
        self.home = home.resolve()
        self.config = config
        self.repositories = {repo.name: repo for repo in config.repositories}
        if len(self.repositories) != len(config.repositories):
            raise GitFailure(
                "config.invalid", "Repository names must be unique"
            )

    def _git(
        self, cwd: Path, *args: str, check: bool = True
    ) -> subprocess.CompletedProcess:
        command = [
            "git",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "core.fsmonitor=false",
            "-c",
            "commit.gpgsign=false",
            "-c",
            "user.name=Pluto Factory",
            "-c",
            "user.email=factory@localhost",
            *args,
        ]
        environment = os.environ.copy()
        for key in (
            "GIT_DIR",
            "GIT_WORK_TREE",
            "GIT_INDEX_FILE",
            "GIT_COMMON_DIR",
        ):
            environment.pop(key, None)
        environment["GIT_TERMINAL_PROMPT"] = "0"
        try:
            result = subprocess.run(
                command,
                check=False,
                cwd=cwd,
                env=environment,
                text=True,
                capture_output=True,
                timeout=self.config.command_timeout_seconds,
            )
        except subprocess.TimeoutExpired as error:
            raise GitFailure(
                "git.timeout", f"Git operation timed out: {args[0]}", True
            ) from error
        except OSError as error:
            raise GitFailure("git.unavailable", str(error)) from error
        if check and result.returncode:
            message = result.stderr.strip() or result.stdout.strip()
            raise GitFailure("git.failed", message[:4000])
        return result

    def _text(self, cwd: Path, *args: str) -> str:
        return self._git(cwd, *args).stdout.strip()

    def _key(self, work_id: str) -> str:
        return "work-" + hashlib.sha256(work_id.encode()).hexdigest()[:20]

    def _record(self, key: str) -> Path:
        return self.home / "git-records" / f"{key}.json"

    def _save(self, entries: list[dict]) -> None:
        if entries:
            atomic_json(
                self._record(entries[0]["work_key"]),
                {
                    "entries": entries,
                },
            )

    def _source(self, repo: Repository) -> Path:
        try:
            source = Path(repo.path).resolve(strict=True)
        except OSError as error:
            raise GitFailure("config.invalid", str(error)) from error
        root = Path(self._text(source, "rev-parse", "--show-toplevel"))
        if root.resolve() != source:
            raise GitFailure(
                "scope.denied", f"{repo.name} must identify a repository root"
            )
        return source

    def _owned(self, path: Path, parent: Path) -> Path:
        resolved = path.resolve()
        if not resolved.is_relative_to(parent.resolve()):
            raise GitFailure("scope.denied", "Path escaped factory ownership")
        return resolved

    def _entry(self, entry: dict) -> tuple[Repository, Path, Path]:
        name = entry["name"]
        if name not in self.repositories:
            raise GitFailure("scope.denied", "Unregistered repository")
        repo = self.repositories[name]
        if Path(entry["source"]).resolve() != self._source(repo):
            raise GitFailure("scope.denied", "Repository identity changed")
        managed = self.home / "repositories" / name
        worktree = self.home / "worktrees" / entry["work_key"] / name
        if (
            Path(entry["managed"]).resolve() != managed.resolve()
            or Path(entry["worktree"]).resolve() != worktree.resolve()
        ):
            raise GitFailure("scope.denied", "Workspace identity changed")
        self._owned(managed, self.home / "repositories")
        self._owned(worktree, self.home / "worktrees")
        if entry.get("access") != repo.access:
            raise GitFailure("scope.denied", "Repository access changed")
        common = Path(self._text(worktree, "rev-parse", "--git-common-dir"))
        if not common.is_absolute():
            common = worktree / common
        if common.resolve() != (managed / ".git").resolve():
            raise GitFailure(
                "scope.denied", "Worktree has an unowned Git store"
            )
        actual = Path(self._text(worktree, "rev-parse", "--show-toplevel"))
        if actual.resolve() != worktree.resolve():
            raise GitFailure("scope.denied", "Worktree root changed")
        return repo, managed, worktree

    def _default_branch(self, source: Path) -> str:
        remote = self._git(
            source,
            "symbolic-ref",
            "--quiet",
            "refs/remotes/origin/HEAD",
            check=False,
        )
        if remote.returncode == 0:
            branch = remote.stdout.strip().removeprefix("refs/remotes/origin/")
            if (
                self._git(
                    source,
                    "show-ref",
                    "--verify",
                    f"refs/heads/{branch}",
                    check=False,
                ).returncode
                == 0
            ):
                return branch
        for branch in ("main", "master"):
            if (
                self._git(
                    source,
                    "show-ref",
                    "--verify",
                    f"refs/heads/{branch}",
                    check=False,
                ).returncode
                == 0
            ):
                return branch
        branch = self._git(
            source, "symbolic-ref", "--quiet", "--short", "HEAD", check=False
        )
        if branch.returncode or not branch.stdout.strip():
            raise GitFailure(
                "config.invalid", "Cannot determine repository default branch"
            )
        return branch.stdout.strip()

    def prepare(self, work_id: str, repository_names: list[str]) -> list[dict]:
        if not repository_names or len(set(repository_names)) != len(
            repository_names
        ):
            raise GitFailure("config.invalid", "Select unique repositories")
        if any(name not in self.repositories for name in repository_names):
            raise GitFailure(
                "scope.denied", "Unregistered repository requested"
            )
        key = self._key(work_id)
        record = self._record(key)
        if record.exists():
            entries = json.loads(record.read_text())["entries"]
            if [entry["name"] for entry in entries] != repository_names:
                raise GitFailure("scope.denied", "Attempt scope changed")
            for entry in entries:
                self._entry(entry)
            return entries
        entries = []
        for name in repository_names:
            repo = self.repositories[name]
            source = self._source(repo)
            managed = self._owned(
                self.home / "repositories" / name,
                self.home / "repositories",
            )
            default = self._default_branch(source)
            if not managed.exists():
                managed.parent.mkdir(parents=True, exist_ok=True)
                self._git(
                    managed.parent,
                    "clone",
                    "--no-local",
                    "--no-hardlinks",
                    "--template=",
                    "--",
                    str(source),
                    str(managed),
                )
                if self._git(
                    managed,
                    "show-ref",
                    "--verify",
                    f"refs/heads/{default}",
                    check=False,
                ).returncode:
                    self._git(
                        managed, "checkout", "-b", default, f"origin/{default}"
                    )
                else:
                    self._git(managed, "checkout", default)
            else:
                origin = self._text(managed, "remote", "get-url", "origin")
                if Path(origin).resolve() != source:
                    raise GitFailure("scope.denied", "Managed clone changed")
            base = self._text(managed, "rev-parse", f"refs/heads/{default}")
            worktree = self._owned(
                self.home / "worktrees" / key / name,
                self.home / "worktrees",
            )
            branch = f"factory/{key}"
            if not worktree.exists():
                worktree.parent.mkdir(parents=True, exist_ok=True)
                if repo.access == "read":
                    self._git(
                        managed,
                        "worktree",
                        "add",
                        "--detach",
                        str(worktree),
                        base,
                    )
                elif (
                    self._git(
                        managed,
                        "show-ref",
                        "--verify",
                        f"refs/heads/{branch}",
                        check=False,
                    ).returncode
                    == 0
                ):
                    self._git(managed, "worktree", "add", str(worktree), branch)
                else:
                    self._git(
                        managed,
                        "worktree",
                        "add",
                        "-b",
                        branch,
                        str(worktree),
                        base,
                    )
            entry = {
                "name": name,
                "source": str(source),
                "managed": str(managed),
                "worktree": str(worktree),
                "branch": branch,
                "default_branch": default,
                "base": base,
                "candidate": base,
                "tree": self._text(managed, "rev-parse", f"{base}^{{tree}}"),
                "access": repo.access,
                "work_key": key,
            }
            self._entry(entry)
            entries.append(entry)
        self._save(entries)
        return entries

    def snapshot(self, entries: list[dict], work_id: str) -> list[dict]:
        result = []
        for original in entries:
            entry = dict(original)
            if entry["work_key"] != self._key(work_id):
                raise GitFailure("scope.denied", "Work item identity changed")
            repo, _, worktree = self._entry(entry)
            if self._text(worktree, "ls-files", "--unmerged"):
                raise GitFailure(
                    "git.conflict", "Resolve merge conflicts first"
                )
            changed = self._text(worktree, "status", "--porcelain")
            if changed and repo.access == "read":
                raise GitFailure("scope.denied", "Read-only repository changed")
            if changed:
                self._git(worktree, "add", "--all")
                self._git(
                    worktree,
                    "commit",
                    "--no-verify",
                    "-m",
                    f"Factory increment for {work_id}",
                )
            entry["candidate"] = self._text(worktree, "rev-parse", "HEAD")
            entry["tree"] = self._text(worktree, "rev-parse", "HEAD^{tree}")
            artifacts = self.home / "artifacts" / entry["work_key"]
            artifacts.mkdir(parents=True, exist_ok=True)
            artifact = artifacts / f"{repo.name}-{entry['candidate']}.patch"
            artifact.write_text(
                self._git(
                    worktree,
                    "diff",
                    "--binary",
                    f"{entry['base']}..{entry['candidate']}",
                ).stdout
            )
            entry["review_artifact"] = str(artifact)
            result.append(entry)
        self._save(result)
        return result

    def _candidate(self, entry: dict) -> None:
        _, _, worktree = self._entry(entry)
        if self._text(worktree, "rev-parse", "HEAD") != entry["candidate"]:
            raise GitFailure(
                "git.candidate_changed", "Candidate revision moved"
            )
        if self._text(worktree, "status", "--porcelain"):
            raise GitFailure(
                "git.candidate_changed", "Candidate has new changes"
            )

    def _validation_roots(
        self, entries: list[dict], candidates: dict[str, str]
    ) -> dict[str, Path]:
        batch = self.home / "validation" / uuid.uuid4().hex
        batch.mkdir(parents=True, exist_ok=False)
        roots = {}
        for entry in entries:
            _, managed, _ = self._entry(entry)
            root = batch / entry["name"]
            self._git(
                managed,
                "worktree",
                "add",
                "--detach",
                str(root),
                candidates[entry["name"]],
            )
            roots[entry["name"]] = root
        return roots

    def _command(
        self, argv: list[str], cwd: Path, environment: dict[str, str]
    ) -> dict:
        started = time.time()
        timed_out = False
        try:
            process = subprocess.Popen(
                argv,
                cwd=cwd,
                env=environment,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
            try:
                stdout, stderr = process.communicate(
                    timeout=self.config.command_timeout_seconds
                )
            except subprocess.TimeoutExpired:
                timed_out = True
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    stdout, stderr = process.communicate(timeout=2)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    stdout, stderr = process.communicate()
            code = 124 if timed_out else process.returncode
        except OSError as error:
            stdout, stderr, code = "", str(error), 127
        return {
            "exit_code": code,
            "stdout": stdout[-100000:],
            "stderr": stderr[-100000:],
            "timed_out": timed_out,
            "seconds": time.time() - started,
        }

    def validate(self, entries: list[dict], kind: str) -> list[dict]:
        return self._validate(entries, kind)

    def _validate(
        self,
        entries: list[dict],
        kind: str,
        candidates: dict[str, str] | None = None,
        integration_plan: dict | None = None,
    ) -> list[dict]:
        if kind not in {"checks", "qa"}:
            raise GitFailure("config.invalid", "Unknown validation phase")
        for entry in entries:
            self._candidate(entry)
        selected = candidates or {
            entry["name"]: entry["candidate"] for entry in entries
        }
        evidence = []
        for entry in entries:
            repo = self.repositories[entry["name"]]
            commands = getattr(repo, kind)
            if repo.access == "read" and not commands:
                continue
            if not commands or any(not command for command in commands):
                raise GitFailure(
                    "validation.unconfigured",
                    f"{repo.name} requires explicit {kind} commands",
                )
            if not repo.flag_environments:
                raise GitFailure(
                    "config.invalid", "Flag matrix cannot be empty"
                )
            for flags in repo.flag_environments:
                roots = self._validation_roots(entries, selected)
                environment = os.environ.copy()
                environment.update(flags)
                for name, root in roots.items():
                    environment[f"FACTORY_REPO_{name.upper()}"] = str(root)
                for command in commands:
                    result = self._command(
                        command, roots[repo.name], environment
                    )
                    mutations = [
                        name
                        for name, root in roots.items()
                        if self._git(
                            root, "diff", "--quiet", "HEAD", "--", check=False
                        ).returncode
                    ]
                    passed = result["exit_code"] == 0 and not mutations
                    managed = Path(entry["managed"])
                    revision = selected[repo.name]
                    row = {
                        "repository": repo.name,
                        "kind": "compatibility" if integration_plan else kind,
                        "profile": kind,
                        "command": command,
                        "environment": flags,
                        "revision": revision,
                        "tree": self._text(
                            managed, "rev-parse", f"{revision}^{{tree}}"
                        ),
                        "candidate_set": selected,
                        "passed": passed,
                        "mutated_repositories": mutations,
                        "validation_root": str(roots[repo.name]),
                        **result,
                    }
                    if integration_plan:
                        row.update(integration_plan)
                    artifact = self.home / "artifacts" / "validation"
                    artifact = artifact / f"{uuid.uuid4().hex}.json"
                    row["artifact"] = str(artifact)
                    atomic_json(artifact, row)
                    evidence.append(row)
        return evidence

    def validate_integration_plan(self, entries: list[dict]) -> list[dict]:
        """Check every intermediate prefix of the actual local merge order.

        The all-new set is already covered by normal checks and QA. Here each
        not-yet-integrated repository remains at its current managed default.
        Original worker worktree identities are never substituted or forged.
        """
        writable = [entry for entry in entries if entry["access"] == "write"]
        if len(writable) < 2:
            return []
        defaults = {}
        for entry in entries:
            self._candidate(entry)
            _, managed, _ = self._entry(entry)
            defaults[entry["name"]] = self._text(
                managed, "rev-parse", f"refs/heads/{entry['default_branch']}"
            )
        order = [entry["name"] for entry in writable]
        evidence = []
        for count in range(1, len(writable)):
            prefix = set(order[:count])
            selected = {
                entry["name"]: (
                    entry["candidate"]
                    if entry["name"] in prefix or entry["access"] == "read"
                    else defaults[entry["name"]]
                )
                for entry in entries
            }
            plan = {
                "integration_prefix": order[:count],
                "integration_order": order,
                "integration_defaults": defaults,
            }
            for kind in ("checks", "qa"):
                evidence.extend(self._validate(entries, kind, selected, plan))
        return evidence

    def sync(self, entries: list[dict]) -> tuple[list[dict], bool]:
        changed = False
        result = []
        for original in entries:
            entry = dict(original)
            repo, managed, worktree = self._entry(entry)
            self._candidate(entry)
            latest = self._text(
                managed, "rev-parse", f"refs/heads/{entry['default_branch']}"
            )
            if latest != entry["base"]:
                if repo.access == "write":
                    merge = self._git(
                        worktree, "merge", "--no-edit", latest, check=False
                    )
                    if merge.returncode:
                        raise GitFailure(
                            "git.conflict", merge.stderr or merge.stdout
                        )
                else:
                    self._git(worktree, "checkout", "--detach", latest)
                entry["base"] = latest
                entry["candidate"] = self._text(worktree, "rev-parse", "HEAD")
                entry["tree"] = self._text(worktree, "rev-parse", "HEAD^{tree}")
                artifacts = self.home / "artifacts" / entry["work_key"]
                artifacts.mkdir(parents=True, exist_ok=True)
                artifact = artifacts / (
                    f"{repo.name}-{entry['candidate']}.patch"
                )
                artifact.write_text(
                    self._git(
                        worktree,
                        "diff",
                        "--binary",
                        f"{entry['base']}..{entry['candidate']}",
                    ).stdout
                )
                entry["review_artifact"] = str(artifact)
                changed = True
            result.append(entry)
        self._save(result)
        return result, changed

    def _contains(self, managed: Path, ancestor: str, descendant: str) -> bool:
        return (
            self._git(
                managed,
                "merge-base",
                "--is-ancestor",
                ancestor,
                descendant,
                check=False,
            ).returncode
            == 0
        )

    def _integration_ready(self, entry: dict) -> tuple[Path, str, bool]:
        _, managed, _ = self._entry(entry)
        self._candidate(entry)
        default = entry["default_branch"]
        if self._text(managed, "branch", "--show-current") != default:
            raise GitFailure("git.default_checkout", "Default checkout changed")
        if self._text(managed, "status", "--porcelain"):
            raise GitFailure(
                "git.dirty_default", "Managed default is not clean"
            )
        current = self._text(managed, "rev-parse", f"refs/heads/{default}")
        integrated = self._contains(managed, entry["candidate"], current)
        if not integrated and current != entry["base"]:
            raise GitFailure(
                "git.base_changed", "Revalidate against new default"
            )
        if not integrated and not self._contains(
            managed, current, entry["candidate"]
        ):
            raise GitFailure(
                "git.not_fast_forward", "Candidate is not integrable"
            )
        return managed, current, integrated

    def integrate(self, entries: list[dict]) -> list[dict]:
        writable = [entry for entry in entries if entry["access"] == "write"]
        result = []
        with ExitStack() as stack:
            for entry in sorted(writable, key=lambda item: item["name"]):
                _, managed, _ = self._entry(entry)
                lock = stack.enter_context(
                    (managed / ".git" / "factory-integration.lock").open("a")
                )
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as error:
                    raise GitFailure(
                        "git.busy", "Another integration owns this repository"
                    ) from error
            for entry in writable:
                self._integration_ready(entry)
            for entry in entries:
                merged = dict(entry)
                if entry["access"] == "write":
                    managed, _, integrated = self._integration_ready(entry)
                    if not integrated:
                        self._git(
                            managed,
                            "merge",
                            "--ff-only",
                            "--no-edit",
                            entry["candidate"],
                        )
                    actual = self._text(managed, "rev-parse", "HEAD")
                    if not self._contains(managed, entry["candidate"], actual):
                        raise GitFailure(
                            "git.merge_uncertain",
                            "Integration needs inspection",
                            True,
                        )
                    merged["merged_revision"] = actual
                    merged["integration"] = "managed-local"
                result.append(merged)
        self._save(result)
        return result

    def cleanup(self, entries: list[dict]) -> None:
        """Remove only clean, integrated worktrees explicitly passed by owner."""
        for entry in entries:
            _, managed, worktree = self._entry(entry)
            current = self._text(
                managed, "rev-parse", f"refs/heads/{entry['default_branch']}"
            )
            if not self._contains(managed, entry["candidate"], current):
                raise GitFailure(
                    "git.not_integrated", "Preserving pending work"
                )
            if self._text(worktree, "status", "--porcelain"):
                raise GitFailure(
                    "git.dirty_worktree", "Preserving local changes"
                )
            self._git(managed, "worktree", "remove", str(worktree))
