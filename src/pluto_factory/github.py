"""GitHub intake and publication with explicit scope and reconciliation.

The adapter never chooses a repository from issue text without checking the
factory registry, and never closes an issue as a side effect of a PR body.
"""

import hashlib
import json
import os
import re
import subprocess
from pathlib import Path

from .config import atomic_json
from .dependencies import DependencyError, source_key
from .github_checks import merge_check_status
from .github_process import classify_failure, git_command, operation_name
from .models import FactoryConfig, Submission

SLUG = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
REVISION = re.compile(r"^[a-fA-F0-9]{40}(?:[a-fA-F0-9]{24})?$")
DECLARATION = re.compile(r"<!--\s*pluto-factory:\s*(.*?)\s*-->", re.DOTALL)
CHECKBOX = re.compile(r"^\s*[-*]\s+\[[ xX]\]\s+(.+?)\s*$", re.MULTILINE)


class GitHubFailure(RuntimeError):
    def __init__(self, code: str, summary: str, uncertain: bool = False):
        super().__init__(summary)
        self.code = code
        self.summary = summary
        self.uncertain = uncertain


class GitHubAdapter:
    def __init__(self, home: Path, config: FactoryConfig, runner=None):
        self.home = Path(home).resolve()
        self.config = config
        self.runner = runner or subprocess.run
        self.rejected_issues = []
        self.repositories = {repo.name: repo for repo in config.repositories}
        self.github = {}
        for repo in config.repositories:
            slug = getattr(repo, "github", "")
            if slug:
                if not SLUG.fullmatch(slug):
                    raise GitHubFailure("config.invalid", "Invalid GitHub slug")
                self.github[repo.name] = slug

    def _run(self, argv, *, cwd=None, mutating=False, acceptable=(0,)):
        environment = os.environ.copy()
        environment.update(
            {
                "GH_HOST": "github.com",
                "GH_PROMPT_DISABLED": "1",
                "GIT_TERMINAL_PROMPT": "0",
            }
        )
        for key in (
            "GIT_DIR",
            "GIT_WORK_TREE",
            "GIT_INDEX_FILE",
            "GIT_COMMON_DIR",
        ):
            environment.pop(key, None)
        try:
            result = self.runner(
                list(argv),
                cwd=cwd or self.home,
                env=environment,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                check=False,
                timeout=self.config.command_timeout_seconds,
            )
        except subprocess.TimeoutExpired as error:
            raise GitHubFailure(
                "github.timeout", f"{operation_name(argv)} timed out", mutating
            ) from error
        except OSError as error:
            raise GitHubFailure(
                "github.unavailable",
                f"{operation_name(argv)} executable unavailable",
                False,
            ) from error
        if result.returncode not in acceptable:
            raise GitHubFailure(
                *classify_failure(
                    argv,
                    result.returncode,
                    result.stderr,
                    mutating=mutating,
                )
            )
        return result

    def _json(self, argv, **kwargs):
        result = self._run(argv, **kwargs)
        try:
            return json.loads(result.stdout)
        except (ValueError, TypeError) as error:
            raise GitHubFailure(
                "github.invalid_response",
                "GitHub returned invalid structured data",
                kwargs.get("mutating", False),
            ) from error

    def _pages(self, value):
        if not isinstance(value, list):
            raise GitHubFailure(
                "github.invalid_response", "Expected GitHub pages"
            )
        rows = []
        for page in value:
            if isinstance(page, dict):
                rows.append(page)
            elif isinstance(page, list) and all(
                isinstance(row, dict) for row in page
            ):
                rows.extend(page)
            else:
                raise GitHubFailure(
                    "github.invalid_response", "Invalid GitHub page contents"
                )
        return rows

    def _api_list(self, endpoint, fields):
        argv = [
            "gh",
            "api",
            endpoint,
            "--hostname",
            "github.com",
            "--method",
            "GET",
            "--paginate",
            "--slurp",
            "-F",
            "per_page=100",
        ]
        for key, value in fields.items():
            argv.extend(["-f", f"{key}={value}"])
        return self._pages(self._json(argv))

    def poll(self) -> list[Submission]:
        submissions = []
        self.rejected_issues = []
        label = getattr(self.config, "github_issue_label", "factory:ready")
        for name, slug in self.github.items():
            issues = self._api_list(
                f"repos/{slug}/issues",
                {
                    "state": "open",
                    "labels": label,
                    "sort": "created",
                    "direction": "asc",
                },
            )
            for issue in issues:
                if "pull_request" in issue:
                    continue
                try:
                    submissions.append(self._submission(name, slug, issue))
                except GitHubFailure as error:
                    if error.code not in {"issue.invalid", "scope.denied"}:
                        raise
                    self.rejected_issues.append(
                        {
                            "source_key": f"github:{slug.lower()}:{issue['number']}",
                            "code": error.code,
                            "summary": error.summary,
                        }
                    )
        return submissions

    def _submission(self, name, slug, issue):
        number = issue.get("number")
        if (
            not isinstance(number, int)
            or isinstance(number, bool)
            or number < 1
        ):
            raise GitHubFailure(
                "github.invalid_response", "Invalid issue number"
            )
        body = issue.get("body") or ""
        title = issue.get("title") or ""
        if not isinstance(body, str) or not isinstance(title, str) or not title:
            raise GitHubFailure("github.invalid_response", "Invalid issue text")
        declarations = DECLARATION.findall(body)
        if len(declarations) > 1:
            raise GitHubFailure(
                "issue.invalid", "Issue has multiple factory declarations"
            )
        targets = [name]
        acceptance = CHECKBOX.findall(body)
        dependencies = []
        if declarations:
            try:
                declaration = json.loads(declarations[0])
            except ValueError as error:
                raise GitHubFailure(
                    "issue.invalid", "Invalid factory declaration"
                ) from error
            if not isinstance(declaration, dict) or not set(
                declaration
            ).issubset({"repositories", "acceptance", "depends_on"}):
                raise GitHubFailure(
                    "issue.invalid", "Unsupported factory declaration"
                )
            targets = declaration.get("repositories", targets)
            acceptance = declaration.get("acceptance", acceptance)
            dependencies = declaration.get("depends_on", dependencies)
        if not isinstance(dependencies, list):
            raise GitHubFailure(
                "issue.invalid", "Issue dependencies must be a list"
            )
        try:
            dependencies = list(
                dict.fromkeys(
                    source_key(reference, self.github.values(), local_slug=slug)
                    for reference in dependencies
                )
            )
        except DependencyError as error:
            raise GitHubFailure("scope.denied", str(error)) from error
        own_source = f"github:{slug.lower()}:{number}"
        if own_source in dependencies:
            raise GitHubFailure(
                "issue.invalid", "Issue cannot depend on itself"
            )
        if (
            not isinstance(targets, list)
            or not targets
            or any(not isinstance(target, str) for target in targets)
            or len(set(targets)) != len(targets)
            or not set(targets).issubset(self.repositories)
        ):
            raise GitHubFailure(
                "scope.denied", "Issue requests unregistered repositories"
            )
        if not any(
            self.repositories[target].access == "write" for target in targets
        ):
            raise GitHubFailure("scope.denied", "Issue has no writable target")
        if not isinstance(acceptance, list) or any(
            not isinstance(item, str) or not item.strip() for item in acceptance
        ):
            raise GitHubFailure("issue.invalid", "Invalid acceptance criteria")
        triage = ""
        if not acceptance:
            acceptance = ["Satisfy issue acceptance and required validation"]
            triage = (
                "\n\nFactory triage: this issue has no explicit acceptance checklist. "
                "Establish observable scope from the request before implementation."
            )
        url = f"https://github.com/{slug}/issues/{number}"
        return Submission(
            title=title,
            instruction=f"Source issue: {url}\n\n{body}{triage}",
            acceptance=acceptance,
            repositories=targets,
            source=f"github:{slug.lower()}",
            delivery_key=str(number),
            depends_on_sources=dependencies,
        )

    def _entry(self, entry):
        name = entry.get("name")
        if name not in self.github:
            raise GitHubFailure(
                "scope.denied", "Repository has no GitHub registration"
            )
        repo = self.repositories[name]
        expected = (self.home / "repositories" / name).resolve()
        managed = Path(entry["managed"]).resolve()
        if managed != expected or not managed.is_relative_to(self.home):
            raise GitHubFailure("scope.denied", "Unowned managed repository")
        if (
            entry.get("source")
            and Path(entry["source"]).resolve() != Path(repo.path).resolve()
        ):
            raise GitHubFailure(
                "scope.denied", "Repository source identity changed"
            )
        return repo, managed, self.github[name]

    def _git(self, managed, *args, mutating=False, acceptable=(0,)):
        return self._run(
            git_command(managed, args),
            mutating=mutating,
            acceptable=acceptable,
        )

    def _text(self, managed, *args):
        return self._git(managed, *args).stdout.strip()

    def refresh_defaults(self, entries) -> None:
        for entry in entries:
            if entry["name"] not in self.github:
                continue
            _, managed, slug = self._entry(entry)
            response = self._json(
                [
                    "gh",
                    "repo",
                    "view",
                    slug,
                    "--json",
                    "defaultBranchRef",
                ]
            )
            default = (response.get("defaultBranchRef") or {}).get("name")
            if not default or default != entry.get("default_branch"):
                raise GitHubFailure(
                    "github.default_changed",
                    "Remote default branch needs reconciliation",
                )
            self._git(managed, "check-ref-format", "--branch", default)
            if self._text(managed, "branch", "--show-current") != default:
                raise GitHubFailure(
                    "github.dirty_default", "Managed default checkout changed"
                )
            if self._text(managed, "status", "--porcelain"):
                raise GitHubFailure(
                    "github.dirty_default", "Managed default is not clean"
                )
            remote_ref = f"refs/remotes/factory/{default}"
            remote = f"https://github.com/{slug}.git"
            self._git(
                managed,
                "fetch",
                "--no-tags",
                "--",
                remote,
                f"+refs/heads/{default}:{remote_ref}",
                mutating=True,
            )
            latest = self._text(managed, "rev-parse", remote_ref)
            current = self._text(managed, "rev-parse", f"refs/heads/{default}")
            if latest == current:
                continue
            ancestor = self._git(
                managed,
                "merge-base",
                "--is-ancestor",
                current,
                latest,
                acceptable=(0, 1),
            )
            if ancestor.returncode:
                raise GitHubFailure(
                    "github.default_diverged",
                    "Managed default cannot fast-forward safely",
                )
            self._git(
                managed,
                "merge",
                "--ff-only",
                "--no-edit",
                latest,
                mutating=True,
            )

    def _record_path(self, work_id):
        key = hashlib.sha256(work_id.encode()).hexdigest()
        return self.home / "github" / "publications" / f"{key}.json"

    def _marker(self, work, name):
        work_id = work["id"]
        if not re.fullmatch(r"[A-Za-z0-9_-]+", work_id):
            raise GitHubFailure("scope.denied", "Invalid work identity")
        return (
            f"<!-- pluto-factory-work:{self.config.name}:{work_id}:{name} -->"
        )

    def _discover(self, slug, branch, base, marker):
        rows = self._api_list(
            f"repos/{slug}/pulls",
            {
                "state": "all",
                "head": f"{slug.split('/')[0]}:{branch}",
                "base": base,
            },
        )
        matches = []
        for row in rows:
            head = row.get("head") or {}
            if head.get("ref") != branch:
                continue
            head_repo = head.get("repo") or {}
            if (
                head_repo
                and head_repo.get("full_name", "").lower() != slug.lower()
            ):
                continue
            if (row.get("base") or {}).get("ref") != base:
                continue
            if marker not in (row.get("body") or ""):
                raise GitHubFailure(
                    "scope.denied", "Factory branch has an unrelated PR"
                )
            matches.append(row)
        opened = [row for row in matches if row.get("state") == "open"]
        if len(opened) > 1:
            raise GitHubFailure(
                "github.ambiguous_pr", "Multiple PRs match this assignment"
            )
        if opened:
            return opened[0]
        return max(matches, key=lambda row: row["number"]) if matches else None

    def _pr_record(self, name, slug, row, expected_head=None):
        head = row.get("head") or {}
        base = row.get("base") or {}
        state = "merged" if row.get("merged_at") else row.get("state", "open")
        return {
            "repository": name,
            "slug": slug,
            "number": row["number"],
            "url": f"https://github.com/{slug}/pull/{row['number']}",
            "branch": head.get("ref"),
            "head": expected_head or head.get("sha"),
            "base": base.get("ref"),
            "state": state,
        }

    def _body(self, work, entry, marker):
        payload = work.get("payload", {})

        def safe_text(value):
            text = re.sub(
                r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\b",
                "address",
                str(value),
                flags=re.IGNORECASE,
            )
            return text.replace("#", "&#35;")

        lines = [
            marker,
            "",
            safe_text(payload.get("title", work.get("title", "Factory work"))),
            "",
        ]
        source = payload.get("source", "")
        issue = payload.get("delivery_key", "")
        if source.startswith("github:") and str(issue).isdigit():
            slug = source.removeprefix("github:")
            if slug in self.github.values():
                lines.extend(
                    [
                        f"Related issue: https://github.com/{slug}/issues/{issue}",
                        "",
                    ]
                )
        lines.extend(["Acceptance:", ""])
        for criterion in payload.get("acceptance", []):
            safe = safe_text(criterion)
            lines.append(f"- {safe}")
        lines.extend(
            ["", "Validation:", "", f"- Candidate: `{entry['candidate']}`"]
        )
        data = work.get("data", {})
        for kind in ("checks", "qa"):
            if (
                data.get(f"{kind}_candidate", {}).get(entry["name"])
                == entry["candidate"]
            ):
                lines.append(
                    f"- Required local {kind} recorded for this candidate"
                )
        for role in ("reviewer", "qa"):
            report = data.get("reports", {}).get(role, {})
            if report.get("outcome"):
                lines.append(
                    f"- {role.capitalize()} report: {report['outcome']}"
                )
        lines.extend(
            [
                "",
                (
                    "Detailed artifacts remain in the factory ledger; they have not "
                    "been uploaded as web-accessible evidence."
                ),
                "Issue completion is coordinated after all required pull requests merge.",
                "",
            ]
        )
        directory = (
            self.home
            / "github"
            / "bodies"
            / hashlib.sha256(work["id"].encode()).hexdigest()
        )
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{entry['name']}.md"
        temporary = path.with_suffix(".tmp")
        with temporary.open("w") as stream:
            stream.write("\n".join(lines))
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
        return path

    def _preflight_publication(self, work, entries):

        # Inspect every writable repository before the first remote mutation.
        for entry in entries:
            if entry.get("access") == "read":
                continue
            repo, managed, slug = self._entry(entry)
            if repo.access != "write":
                raise GitHubFailure("scope.denied", "Repository is read-only")
            if not entry.get("branch", "").startswith("factory/"):
                raise GitHubFailure("scope.denied", "Invalid factory branch")
            self._git(managed, "check-ref-format", "--branch", entry["branch"])
            candidate, base = entry.get("candidate", ""), entry.get("base", "")
            if not REVISION.fullmatch(candidate) or not REVISION.fullmatch(
                base
            ):
                raise GitHubFailure(
                    "github.invalid_candidate",
                    "Publication needs frozen base and candidate commits",
                )
            result = self._git(
                managed,
                "diff",
                "--quiet",
                base,
                candidate,
                "--",
                acceptable=(0, 1),
            )
            if result.returncode == 0:
                existing = self._discover(
                    slug,
                    entry["branch"],
                    entry["default_branch"],
                    self._marker(work, entry["name"]),
                )
                if (
                    existing
                    and existing.get("merged_at")
                    and existing.get("head", {}).get("sha") == candidate
                ):
                    continue
                raise GitHubFailure(
                    "github.no_changes",
                    "No changes to publish; reconcile prior merges and acceptance before retrying",
                    False,
                )

    def publish(self, work, entries) -> list[dict]:
        self._preflight_publication(work, entries)
        for entry in entries:
            if entry.get("access") != "read":
                self._entry(entry)
        path = self._record_path(work["id"])
        record = (
            json.loads(path.read_text())
            if path.exists()
            else {"repositories": {}}
        )
        published = []
        for entry in entries:
            if entry.get("access") == "read":
                continue
            repo, managed, slug = self._entry(entry)
            if repo.access != "write":
                raise GitHubFailure("scope.denied", "Repository is read-only")
            candidate = entry["candidate"]
            branch = entry["branch"]
            if not REVISION.fullmatch(candidate) or not branch.startswith(
                "factory/"
            ):
                raise GitHubFailure(
                    "scope.denied", "Invalid factory publication identity"
                )
            self._git(managed, "check-ref-format", "--branch", branch)
            actual = self._text(
                managed, "rev-parse", "--verify", f"{candidate}^{{commit}}"
            )
            if actual != candidate:
                raise GitHubFailure(
                    "github.candidate_changed", "Candidate identity changed"
                )
            marker = self._marker(work, entry["name"])
            existing = self._discover(
                slug, branch, entry["default_branch"], marker
            )
            if (
                existing
                and existing.get("merged_at")
                and existing["head"]["sha"] == candidate
            ):
                published.append(
                    self._pr_record(entry["name"], slug, existing, candidate)
                )
                continue
            if (
                existing
                and existing.get("state") == "closed"
                and not existing.get("merged_at")
            ):
                raise GitHubFailure(
                    "github.pr_closed",
                    "Existing factory PR was closed without merge",
                )
            remote = f"https://github.com/{slug}.git"
            ref = f"refs/heads/{branch}"
            observed = self._text(managed, "ls-remote", "--heads", remote, ref)
            remote_head = observed.split()[0] if observed else ""
            previous = record["repositories"].get(entry["name"], {})
            if previous and previous.get("slug", slug) != slug:
                raise GitHubFailure(
                    "scope.denied", "Publication repository changed"
                )
            if (
                remote_head
                and not existing
                and (
                    previous.get("desired_head") != candidate
                    or previous.get("branch") != branch
                )
            ):
                raise GitHubFailure(
                    "github.head_changed",
                    "Existing branch has no owned publication",
                )
            if remote_head and remote_head != candidate:
                owned = existing and marker in existing.get("body", "")
                expected = previous.get("head")
                if not owned or (expected and expected != remote_head):
                    raise GitHubFailure(
                        "github.head_changed",
                        "Remote branch ownership or head changed",
                    )
                ancestor = self._git(
                    managed,
                    "merge-base",
                    "--is-ancestor",
                    remote_head,
                    candidate,
                    acceptable=(0, 1),
                )
                if ancestor.returncode:
                    raise GitHubFailure(
                        "github.head_changed",
                        "Remote branch is not a candidate ancestor",
                    )
            record["repositories"][entry["name"]] = {
                "slug": slug,
                "branch": branch,
                "head": remote_head,
                "desired_head": candidate,
                "state": "push_pending",
            }
            atomic_json(path, record)
            if remote_head != candidate:
                self._git(
                    managed,
                    "push",
                    f"--force-with-lease={ref}:{remote_head}",
                    "--",
                    remote,
                    f"{candidate}:{ref}",
                    mutating=True,
                )
            record["repositories"][entry["name"]].update(
                {
                    "head": candidate,
                    "state": "pushed",
                }
            )
            atomic_json(path, record)
            body = self._body(work, entry, marker)
            title = (
                work.get("payload", {}).get("title")
                or work.get("title")
                or "Factory work"
            )
            if existing and not existing.get("merged_at"):
                if (
                    existing.get("body") != body.read_text()
                    or existing.get("title") != title
                ):
                    self._run(
                        [
                            "gh",
                            "pr",
                            "edit",
                            str(existing["number"]),
                            "--repo",
                            slug,
                            "--title",
                            title,
                            "--body-file",
                            str(body),
                        ],
                        mutating=True,
                    )
                pull_request = self._pr_record(
                    entry["name"], slug, existing, candidate
                )
            else:
                result = self._run(
                    [
                        "gh",
                        "pr",
                        "create",
                        "--repo",
                        slug,
                        "--base",
                        entry["default_branch"],
                        "--head",
                        branch,
                        "--title",
                        title,
                        "--body-file",
                        str(body),
                        "--no-maintainer-edit",
                    ],
                    mutating=True,
                )
                match = re.fullmatch(
                    rf"https://github\.com/{re.escape(slug)}/pull/(\d+)",
                    result.stdout.strip(),
                )
                if not match:
                    raise GitHubFailure(
                        "github.pr_pending",
                        "PR creation needs reconciliation",
                        True,
                    )
                pull_request = {
                    "repository": entry["name"],
                    "slug": slug,
                    "number": int(match.group(1)),
                    "url": result.stdout.strip(),
                    "branch": branch,
                    "head": candidate,
                    "base": entry["default_branch"],
                    "state": "open",
                }
            record["repositories"][entry["name"]].update(
                {
                    "state": "published",
                    "pull_request": pull_request,
                }
            )
            atomic_json(path, record)
            published.append(pull_request)
        return published

    def _pr_scope(self, pull_request):
        name = pull_request.get("repository")
        if name not in self.github or self.github[name] != pull_request.get(
            "slug"
        ):
            raise GitHubFailure(
                "scope.denied", "Unregistered pull request repository"
            )
        number = pull_request.get("number")
        if (
            not isinstance(number, int)
            or isinstance(number, bool)
            or number < 1
        ):
            raise GitHubFailure("scope.denied", "Invalid pull request identity")
        return self.github[name], number

    def default_branch_checks(self, pull_request) -> dict:
        from .github_delivery import default_branch_checks

        return default_branch_checks(self, pull_request)

    def poll_pull_requests(self, prs) -> dict:
        results = []
        for pull_request in prs:
            slug, number = self._pr_scope(pull_request)
            row = self._json(
                [
                    "gh",
                    "pr",
                    "view",
                    str(number),
                    "--repo",
                    slug,
                    "--json",
                    (
                        "number,title,url,state,headRefOid,headRefName,baseRefName,"
                        "reviewDecision,statusCheckRollup,mergeStateStatus,mergedAt,"
                        "isDraft,autoMergeRequest,mergeCommit"
                    ),
                ]
            )
            if row.get("headRefName") != pull_request.get("branch") or row.get(
                "baseRefName"
            ) != pull_request.get("base"):
                raise GitHubFailure(
                    "github.pr_changed", "Pull request branch identity changed"
                )
            state = str(row.get("state", "OPEN")).lower()
            failed_checks = any(
                str(check.get("conclusion") or check.get("state") or "").upper()
                in {
                    "FAILURE",
                    "ERROR",
                    "TIMED_OUT",
                    "ACTION_REQUIRED",
                    "STARTUP_FAILURE",
                }
                for check in row.get("statusCheckRollup") or []
            )
            if row.get("mergedAt") or state == "merged":
                state = "merged"
            elif (
                row.get("reviewDecision") == "CHANGES_REQUESTED"
                or failed_checks
            ) and state == "open":
                state = "changes_requested"
            elif state not in {"open", "closed"}:
                raise GitHubFailure(
                    "github.invalid_response", "Unknown pull request state"
                )
            merge_commit = row.get("mergeCommit")
            results.append(
                {
                    **pull_request,
                    "merge_commit": merge_commit.get("oid")
                    if isinstance(merge_commit, dict)
                    else None,
                    "merged_at": row.get("mergedAt"),
                    "state": state,
                    "title": row.get("title", "Factory work"),
                    "observed_head": row.get("headRefOid"),
                    "head_changed": row.get("headRefOid")
                    != pull_request.get("head"),
                    "review_decision": row.get("reviewDecision"),
                    "checks": row.get("statusCheckRollup") or [],
                    "merge_state": row.get("mergeStateStatus"),
                    "is_draft": bool(row.get("isDraft")),
                    "auto_merge_enabled": bool(row.get("autoMergeRequest")),
                }
            )
        states = {row["state"] for row in results}
        if results and states == {"merged"}:
            state = "merged"
        elif "closed" in states:
            state = "closed"
        elif "changes_requested" in states:
            state = "changes_requested"
        else:
            state = "open"
        return {"state": state, "pull_requests": results}

    def request_merge(self, prs) -> dict:
        if not getattr(self.config, "github_auto_merge", False):
            return {
                "requested": False,
                "state": "disabled",
                "pull_requests": prs,
            }
        status = self.poll_pull_requests(prs)
        if status["state"] in {"closed", "changes_requested"}:
            return {"requested": False, **status}
        requested = False
        for pull_request in status["pull_requests"]:
            if pull_request["state"] == "merged":
                continue
            if pull_request["head_changed"] or pull_request["is_draft"]:
                return {"requested": False, **status}
            checks = merge_check_status(
                pull_request,
                getattr(self.config, "github_required_checks", []),
            )
            if not checks["ready"]:
                return {"requested": False, "check_gate": checks, **status}
            if pull_request["auto_merge_enabled"]:
                break
            slug, number = self._pr_scope(pull_request)
            self._run(
                [
                    "gh",
                    "pr",
                    "merge",
                    str(number),
                    "--repo",
                    slug,
                    "--auto",
                    "--squash",
                    "--match-head-commit",
                    pull_request["head"],
                ],
                mutating=True,
            )
            requested = True
            break
        return {"requested": requested, **status}

    def complete_sources(self, work) -> dict:
        if not getattr(self.config, "close_source_issues", False):
            return {"state": "disabled"}
        payload = work.get("payload", {})
        source = payload.get("source", "")
        if not source.startswith("github:"):
            return {"state": "not_github"}
        slug = source.removeprefix("github:")
        number = str(payload.get("delivery_key", ""))
        if (
            slug not in self.github.values()
            or not number.isdigit()
            or int(number) < 1
        ):
            raise GitHubFailure(
                "scope.denied", "Issue source is outside factory scope"
            )
        prs = work.get("data", {}).get("pull_requests", [])
        if not prs:
            return {"state": "pending"}
        status = self.poll_pull_requests(prs)
        if status["state"] != "merged" or any(
            item["head_changed"] for item in status["pull_requests"]
        ):
            return {"state": "pending"}
        issue = self._json(
            [
                "gh",
                "issue",
                "view",
                number,
                "--repo",
                slug,
                "--json",
                "state,url,title",
            ]
        )
        if str(issue.get("state", "")).lower() != "closed":
            self._run(
                [
                    "gh",
                    "issue",
                    "close",
                    number,
                    "--repo",
                    slug,
                    "--reason",
                    "completed",
                ],
                mutating=True,
            )
        return {
            "state": "closed",
            "url": f"https://github.com/{slug}/issues/{number}",
        }
