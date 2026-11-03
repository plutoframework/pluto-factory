"""Offline GitHub adapter tests: every command is an injected fake process."""

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from pluto_factory.github import GitHubAdapter, GitHubFailure

OLD = "1" * 40
NEW = "2" * 40
OTHER = "3" * 40


class Remote:
    def __init__(self):
        self.calls = []
        self.issues = {}
        self.prs = {}
        self.heads = {}
        self.defaults = {}
        self.local_defaults = {}
        self.views = {}
        self.closed_issues = []
        self.timeout_create = False
        self.timeout_push = False
        self.diverged = False
        self.dirty = False
        self.empty_diffs = set()
        self.default_branch = "main"

    def __call__(self, argv, **kwargs):
        self.calls.append(argv)
        assert kwargs["check"] is False
        assert kwargs["stdin"] == subprocess.DEVNULL
        assert kwargs["env"]["GH_PROMPT_DISABLED"] == "1"
        output = ""
        code = 0
        if argv[0] == "git":
            managed = argv[argv.index("-C") + 1]
            args = argv[argv.index("-C") + 2 :]
            operation = args[0]
            if operation == "rev-parse":
                reference = args[-1]
                if reference.endswith("^{commit}"):
                    output = reference.removesuffix("^{commit}")
                elif reference.startswith("refs/remotes/factory/"):
                    output = self.defaults.get(managed, NEW)
                else:
                    output = self.local_defaults.get(managed, OLD)
            elif operation == "branch":
                output = "main"
            elif operation == "status":
                output = "?? valuable.txt" if self.dirty else ""
            elif operation == "ls-remote":
                value = self.heads.get((args[-2], args[-1]))
                output = f"{value}\t{args[-1]}\n" if value else ""
            elif operation == "push":
                remote, specification = args[-2:]
                revision, reference = specification.split(":", 1)
                lease = next(
                    value
                    for value in args
                    if value.startswith("--force-with-lease=")
                )
                expected = lease.split("=", 1)[1].split(":", 1)[1]
                assert self.heads.get((remote, reference), "") == expected
                self.heads[(remote, reference)] = revision
                if self.timeout_push:
                    self.timeout_push = False
                    raise subprocess.TimeoutExpired(argv, 1)
            elif operation == "diff":
                code = 0 if managed in self.empty_diffs else 1
            elif operation == "merge-base":
                code = 1 if self.diverged else 0
            elif operation == "merge":
                self.local_defaults[managed] = args[-1]
        elif argv[1] == "api":
            endpoint = argv[2]
            slug = "/".join(endpoint.split("/")[1:3])
            if endpoint.endswith("/issues"):
                output = json.dumps(self.issues.get(slug, [[]]))
            elif endpoint.endswith("/pulls"):
                head = next(
                    value for value in argv if value.startswith("head=")
                )
                branch = head.split(":", 1)[1]
                output = json.dumps([self.prs.get((slug, branch), [])])
        elif argv[1:3] == ["repo", "view"]:
            output = json.dumps(
                {"defaultBranchRef": {"name": self.default_branch}}
            )
        elif argv[1:3] == ["pr", "create"]:
            slug = argv[argv.index("--repo") + 1]
            branch = argv[argv.index("--head") + 1]
            base = argv[argv.index("--base") + 1]
            body = Path(argv[argv.index("--body-file") + 1]).read_text()
            title = argv[argv.index("--title") + 1]
            number = len(self.views) + 1
            head = self.heads[
                (f"https://github.com/{slug}.git", f"refs/heads/{branch}")
            ]
            row = {
                "number": number,
                "body": body,
                "title": title,
                "state": "open",
                "head": {
                    "ref": branch,
                    "sha": head,
                    "repo": {"full_name": slug},
                },
                "base": {"ref": base},
                "merged_at": None,
            }
            self.prs.setdefault((slug, branch), []).append(row)
            self.views[(slug, number)] = {
                "number": number,
                "state": "OPEN",
                "title": title,
                "headRefName": branch,
                "headRefOid": head,
                "baseRefName": base,
                "reviewDecision": "APPROVED",
                "statusCheckRollup": [],
                "mergeStateStatus": "CLEAN",
                "mergedAt": None,
                "isDraft": False,
            }
            output = f"https://github.com/{slug}/pull/{number}\n"
            if self.timeout_create:
                self.timeout_create = False
                raise subprocess.TimeoutExpired(argv, 1)
        elif argv[1:3] == ["pr", "view"]:
            slug = argv[argv.index("--repo") + 1]
            output = json.dumps(self.views[(slug, int(argv[3]))])
        elif argv[1:3] == ["pr", "merge"]:
            slug = argv[argv.index("--repo") + 1]
            self.views[(slug, int(argv[3]))]["autoMergeRequest"] = {
                "enabledAt": "recorded"
            }
        elif argv[1:3] == ["issue", "view"]:
            identity = (argv[argv.index("--repo") + 1], int(argv[3]))
            state = "CLOSED" if identity in self.closed_issues else "OPEN"
            output = json.dumps({"state": state, "title": "Issue"})
        elif argv[1:3] == ["issue", "close"]:
            self.closed_issues.append(
                (argv[argv.index("--repo") + 1], int(argv[3]))
            )
        return subprocess.CompletedProcess(argv, code, output, "")


def adapter(tmp_path, **settings):
    home = tmp_path / "factory"
    home.mkdir()
    repositories = [
        SimpleNamespace(
            name=name,
            path=str(tmp_path / name),
            access="write",
            github=f"owner/{name}",
        )
        for name in ("core", "cli")
    ]
    config = SimpleNamespace(
        name="example",
        repositories=repositories,
        command_timeout_seconds=5,
        github_issue_label="factory:ready",
        github_auto_merge=False,
        close_source_issues=False,
    )
    for key, value in settings.items():
        setattr(config, key, value)
    remote = Remote()
    return GitHubAdapter(home, config, runner=remote), remote


def entries(integration, names=("core",)):
    return [
        {
            "name": name,
            "source": integration.repositories[name].path,
            "managed": str(integration.home / "repositories" / name),
            "access": "write",
            "candidate": NEW,
            "base": OLD,
            "branch": "factory/work-example",
            "default_branch": "main",
        }
        for name in names
    ]


def work():
    return {
        "id": "work-example",
        "title": "Repair calculator",
        "payload": {
            "title": "Repair calculator",
            "source": "github:owner/core",
            "delivery_key": "12",
            "acceptance": ["Addition returns the sum"],
        },
        "data": {
            "checks_candidate": {"core": NEW, "cli": NEW},
            "qa_candidate": {"core": NEW, "cli": NEW},
            "reports": {"reviewer": {"outcome": "ready"}},
        },
    }


def test_poll_reads_every_page_and_validates_cross_repository_declarations(
    tmp_path,
):
    integration, remote = adapter(tmp_path)
    first_page = [
        {
            "number": number,
            "title": f"Task {number}",
            "body": "- [ ] Pass tests",
        }
        for number in range(1, 101)
    ]
    remote.issues["owner/core"] = [
        first_page,
        [
            {
                "number": 101,
                "title": "Cross-repository task",
                "body": '<!-- pluto-factory:{"repositories":["core","cli"],"acceptance":["Works together"]} -->',
            }
        ],
    ]
    submissions = integration.poll()
    assert len(submissions) == 101
    assert submissions[-1].repositories == ["core", "cli"]
    assert submissions[-1].acceptance == ["Works together"]
    assert submissions[-1].source == "github:owner/core"
    assert submissions[-1].delivery_key == "101"
    assert (
        "https://github.com/owner/core/issues/101"
        in submissions[-1].instruction
    )
    assert "--paginate" in remote.calls[0] and "--slurp" in remote.calls[0]
    assert "per_page=100" in remote.calls[0]


def test_poll_keeps_ambiguous_issue_for_explicit_triage_and_ignores_prs(
    tmp_path,
):
    integration, remote = adapter(tmp_path)
    remote.issues["owner/core"] = [
        [
            {
                "number": 1,
                "title": "Investigate",
                "body": "Something is broken",
            },
            {"number": 2, "title": "PR", "pull_request": {}, "body": ""},
        ]
    ]
    submissions = integration.poll()
    assert len(submissions) == 1
    assert "triage" in submissions[0].instruction
    assert submissions[0].acceptance == [
        "Satisfy issue acceptance and required validation"
    ]


def test_poll_rejects_issue_text_routing_outside_registered_repositories(
    tmp_path,
):
    integration, remote = adapter(tmp_path)
    remote.issues["owner/core"] = [
        [
            {
                "number": 1,
                "title": "Task",
                "body": '<!-- pluto-factory:{"repositories":["outside"]} -->',
            }
        ]
    ]
    assert integration.poll() == []
    assert integration.rejected_issues[0]["code"] == "scope.denied"


def test_publish_creates_one_owned_pr_and_reconciles_duplicate_dispatch(
    tmp_path,
):
    integration, remote = adapter(tmp_path)
    item = work()
    published = integration.publish(item, entries(integration))
    assert published[0]["head"] == NEW
    assert integration.publish(item, entries(integration)) == published
    creations = [
        call for call in remote.calls if call[:3] == ["gh", "pr", "create"]
    ]
    pushes = [call for call in remote.calls if "push" in call]
    assert len(creations) == len(pushes) == 1
    assert "--body-file" in creations[0] and "--body" not in creations[0]
    assert "--head" in creations[0] and "--no-maintainer-edit" in creations[0]
    assert "--force-with-lease=refs/heads/factory/work-example:" in pushes[0]
    assert "https://github.com/owner/core.git" in pushes[0]
    body = Path(creations[0][creations[0].index("--body-file") + 1]).read_text()
    assert "Addition returns the sum" in body
    assert str(integration.home) not in body
    assert "Related issue:" in body and "Closes #" not in body


@pytest.mark.parametrize("operation", ["push", "create"])
def test_uncertain_mutation_reconciles_before_repeating(tmp_path, operation):
    integration, remote = adapter(tmp_path)
    setattr(remote, f"timeout_{operation}", True)
    with pytest.raises(GitHubFailure) as error:
        integration.publish(work(), entries(integration))
    assert error.value.uncertain
    integration.publish(work(), entries(integration))
    assert len([call for call in remote.calls if "push" in call]) == 1
    assert (
        len(
            [
                call
                for call in remote.calls
                if call[:3] == ["gh", "pr", "create"]
            ]
        )
        == 1
    )


def test_publish_never_overwrites_an_unowned_remote_branch(tmp_path):
    integration, remote = adapter(tmp_path)
    remote.heads[
        ("https://github.com/owner/core.git", "refs/heads/factory/work-example")
    ] = OTHER
    with pytest.raises(GitHubFailure) as error:
        integration.publish(work(), entries(integration))
    assert error.value.code == "github.head_changed"
    assert not any("push" in call for call in remote.calls)


def test_publish_rejects_forged_managed_path_before_remote_commands(tmp_path):
    integration, remote = adapter(tmp_path)
    targets = entries(integration)
    targets[0]["managed"] = str(tmp_path / "unowned")
    with pytest.raises(GitHubFailure) as error:
        integration.publish(work(), targets)
    assert error.value.code == "scope.denied"
    assert remote.calls == []


def test_pr_body_cannot_prematurely_close_source_issue(tmp_path):
    integration, remote = adapter(tmp_path)
    item = work()
    item["payload"]["title"] = "Fixes #12"
    item["payload"]["acceptance"] = [
        "Closes https://github.com/owner/core/issues/12"
    ]
    integration.publish(item, entries(integration))
    creation = next(
        call for call in remote.calls if call[:3] == ["gh", "pr", "create"]
    )
    body = Path(creation[creation.index("--body-file") + 1]).read_text()
    assert "Fixes #12" not in body
    assert "Closes https://" not in body


def test_auto_merge_requires_owner_policy_and_uses_expected_head(tmp_path):
    integration, remote = adapter(tmp_path)
    published = integration.publish(work(), entries(integration))
    assert integration.request_merge(published)["requested"] is False
    assert not any(call[:3] == ["gh", "pr", "merge"] for call in remote.calls)
    integration.config.github_auto_merge = True
    assert integration.request_merge(published)["requested"]
    request = next(
        call for call in remote.calls if call[:3] == ["gh", "pr", "merge"]
    )
    assert "--auto" in request and "--admin" not in request
    assert request[request.index("--match-head-commit") + 1] == NEW
    assert integration.request_merge(published)["requested"] is False
    assert (
        len(
            [call for call in remote.calls if call[:3] == ["gh", "pr", "merge"]]
        )
        == 1
    )


def test_auto_merge_advances_in_validated_repository_order(tmp_path):
    integration, remote = adapter(tmp_path, github_auto_merge=True)
    published = integration.publish(
        work(), entries(integration, ("core", "cli"))
    )
    integration.request_merge(published)
    requests = [
        call for call in remote.calls if call[:3] == ["gh", "pr", "merge"]
    ]
    assert len(requests) == 1
    assert requests[0][requests[0].index("--repo") + 1] == "owner/core"
    integration.request_merge(published)
    assert (
        len(
            [call for call in remote.calls if call[:3] == ["gh", "pr", "merge"]]
        )
        == 1
    )
    first = remote.views[("owner/core", published[0]["number"])]
    first["state"] = "MERGED"
    first["mergedAt"] = "2026-11-01T00:00:00Z"
    integration.request_merge(published)
    requests = [
        call for call in remote.calls if call[:3] == ["gh", "pr", "merge"]
    ]
    assert len(requests) == 2
    assert requests[-1][requests[-1].index("--repo") + 1] == "owner/cli"


def test_failed_remote_check_requests_revision_instead_of_merge(tmp_path):
    integration, remote = adapter(tmp_path, github_auto_merge=True)
    published = integration.publish(work(), entries(integration))
    view = remote.views[("owner/core", published[0]["number"])]
    view["statusCheckRollup"] = [
        {"conclusion": "FAILURE", "name": "Required CI"}
    ]
    assert (
        integration.poll_pull_requests(published)["state"]
        == "changes_requested"
    )
    assert not integration.request_merge(published)["requested"]


@pytest.mark.parametrize("condition", ["head_changed", "changes_requested"])
def test_remote_changes_prevent_merge_requests(tmp_path, condition):
    integration, remote = adapter(tmp_path, github_auto_merge=True)
    published = integration.publish(work(), entries(integration))
    view = remote.views[("owner/core", published[0]["number"])]
    if condition == "head_changed":
        view["headRefOid"] = OTHER
    else:
        view["reviewDecision"] = "CHANGES_REQUESTED"
    assert integration.request_merge(published)["requested"] is False
    assert not any(call[:3] == ["gh", "pr", "merge"] for call in remote.calls)


def test_source_issue_closes_only_after_every_repository_pr_merged(tmp_path):
    integration, remote = adapter(tmp_path, close_source_issues=True)
    item = work()
    published = integration.publish(item, entries(integration, ("core", "cli")))
    item["data"]["pull_requests"] = published
    remote.views[("owner/core", published[0]["number"])]["state"] = "MERGED"
    assert integration.complete_sources(item)["state"] == "pending"
    assert remote.closed_issues == []
    remote.views[("owner/cli", published[1]["number"])]["state"] = "MERGED"
    assert integration.complete_sources(item)["state"] == "closed"
    assert remote.closed_issues == [("owner/core", 12)]
    assert integration.complete_sources(item)["state"] == "closed"
    assert remote.closed_issues == [("owner/core", 12)]


def test_different_merged_head_cannot_close_issue(tmp_path):
    integration, remote = adapter(tmp_path, close_source_issues=True)
    item = work()
    published = integration.publish(item, entries(integration))
    item["data"]["pull_requests"] = published
    view = remote.views[("owner/core", published[0]["number"])]
    view.update({"state": "MERGED", "headRefOid": OTHER})
    assert integration.complete_sources(item)["state"] == "pending"
    assert remote.closed_issues == []


def test_refresh_uses_canonical_remote_and_fast_forwards_only_owned_default(
    tmp_path,
):
    integration, remote = adapter(tmp_path)
    target = entries(integration)[0]
    integration.refresh_defaults([target])
    assert remote.local_defaults[target["managed"]] == NEW
    fetch = next(call for call in remote.calls if "fetch" in call)
    assert "https://github.com/owner/core.git" in fetch
    assert "+refs/heads/main:refs/remotes/factory/main" in fetch
    assert not any("reset" in call for call in remote.calls)


@pytest.mark.parametrize(
    "condition", ["dirty", "diverged", "different_default"]
)
def test_refresh_refuses_unsafe_managed_defaults(tmp_path, condition):
    integration, remote = adapter(tmp_path)
    if condition == "different_default":
        remote.default_branch = "trunk"
    else:
        setattr(remote, condition, True)
    with pytest.raises(GitHubFailure):
        integration.refresh_defaults(entries(integration))
    assert not any("merge" in call or "reset" in call for call in remote.calls)


def test_errors_never_expose_remote_stderr(tmp_path):
    integration, _ = adapter(tmp_path)

    def failed(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 1, "", "token=super-secret")

    integration.runner = failed
    with pytest.raises(GitHubFailure) as error:
        integration.poll()
    assert "super-secret" not in str(error.value)
    assert error.value.uncertain is False


def test_issue_metadata_normalizes_local_and_cross_repository_dependencies(
    tmp_path,
):
    integration, remote = adapter(tmp_path)
    remote.issues["owner/core"] = [
        [
            {
                "number": 5,
                "title": "Follow prerequisites",
                "body": '<!-- pluto-factory:{"depends_on":["#2","OWNER/CLI#003","owner/cli#3"]} -->',
            }
        ]
    ]
    submitted = integration.poll()[0]
    assert submitted.depends_on_sources == [
        "github:owner/core:2",
        "github:owner/cli:3",
    ]
    assert submitted.depends_on == []


@pytest.mark.parametrize(
    "reference", ["other/private#2", "#5", "#0", "#" + "9" * 100]
)
def test_invalid_or_self_issue_dependencies_are_rejected(tmp_path, reference):
    integration, remote = adapter(tmp_path)
    remote.issues["owner/core"] = [
        [
            {
                "number": 5,
                "title": "Invalid prerequisite",
                "body": "<!-- pluto-factory:"
                + json.dumps({"depends_on": [reference]})
                + " -->",
            }
        ]
    ]
    assert integration.poll() == []
    assert len(integration.rejected_issues) == 1


def test_invalid_issue_does_not_starve_independent_issue_intake(tmp_path):
    integration, remote = adapter(tmp_path)
    remote.issues["owner/core"] = [
        [
            {
                "number": 1,
                "title": "Self-dependent",
                "body": '<!-- pluto-factory:{"depends_on":["#1"]} -->',
            },
            {"number": 2, "title": "Independent", "body": "- [ ] Pass checks"},
        ]
    ]
    submissions = integration.poll()
    assert [item.delivery_key for item in submissions] == ["2"]
    assert integration.rejected_issues[0]["source_key"] == "github:owner/core:1"


@pytest.mark.parametrize(
    "check,expected",
    [
        (None, False),
        ({"name": "validate", "status": "IN_PROGRESS"}, False),
        (
            {
                "name": "validate",
                "status": "COMPLETED",
                "conclusion": "SUCCESS",
            },
            True,
        ),
        (
            {
                "name": "validate",
                "status": "COMPLETED",
                "conclusion": "FAILURE",
            },
            False,
        ),
        (
            {
                "name": "validate",
                "status": "COMPLETED",
                "conclusion": "SKIPPED",
            },
            False,
        ),
    ],
)
def test_auto_merge_waits_for_named_ci_success(tmp_path, check, expected):
    integration, remote = adapter(
        tmp_path, github_auto_merge=True, github_required_checks=["validate"]
    )
    published = integration.publish(work(), entries(integration))
    view = remote.views[("owner/core", published[0]["number"])]
    view["statusCheckRollup"] = [check] if check else []
    result = integration.request_merge(published)
    assert result["requested"] is expected
    merge_calls = [
        call for call in remote.calls if call[:3] == ["gh", "pr", "merge"]
    ]
    assert bool(merge_calls) is expected
    if expected:
        assert "--auto" in merge_calls[0]
        assert (
            merge_calls[0][merge_calls[0].index("--match-head-commit") + 1]
            == NEW
        )


def test_optional_pending_ci_also_prevents_unprotected_immediate_merge(
    tmp_path,
):
    integration, remote = adapter(tmp_path, github_auto_merge=True)
    published = integration.publish(work(), entries(integration))
    view = remote.views[("owner/core", published[0]["number"])]
    view["statusCheckRollup"] = [{"name": "some-ci", "status": "QUEUED"}]
    result = integration.request_merge(published)
    assert not result["requested"]
    assert result["check_gate"]["pending"] == ["some-ci"]
    assert not any(call[:3] == ["gh", "pr", "merge"] for call in remote.calls)


def test_publication_git_credentials_use_existing_gh_login_without_token_argv(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("GH_TOKEN", "sentinel-existing-credential")
    integration, remote = adapter(tmp_path)
    integration.publish(work(), entries(integration))
    commands = [call for call in remote.calls if call[0] == "git"]
    assert commands
    for argv in commands:
        assert "credential.helper=" in argv
        assert (
            "credential.https://github.com.helper=!gh auth git-credential"
            in argv
        )
        assert "core.hooksPath=/dev/null" in argv
        assert "sentinel-existing-credential" not in " ".join(argv)
        assert "--global" not in argv


@pytest.mark.parametrize(
    "diagnostic,code,uncertain",
    [
        (
            "fatal: could not read Username for 'https://github.com': terminal prompts disabled",
            "auth.required",
            False,
        ),
        (
            "remote: Permission to owner/core.git denied to user.",
            "permission.required",
            False,
        ),
        (
            "fatal: Recv failure: Connection reset by peer",
            "github.operation_failed",
            True,
        ),
    ],
)
def test_git_push_failure_categories_are_sanitized_and_preserve_transport_uncertainty(
    tmp_path,
    diagnostic,
    code,
    uncertain,
):
    integration, _ = adapter(tmp_path)

    def fail(argv, **kwargs):
        return subprocess.CompletedProcess(
            argv, 128, "", diagnostic + "\nsentinel-credential"
        )

    integration.runner = fail
    with pytest.raises(GitHubFailure) as error:
        integration._git(
            integration.home / "repositories" / "core",
            "push",
            "https://github.com/owner/core.git",
            "candidate:refs/heads/task",
            mutating=True,
        )
    assert error.value.code == code
    assert error.value.uncertain is uncertain
    assert "Git push" in error.value.summary
    assert "sentinel" not in str(error.value)


@pytest.mark.parametrize("empty_index", [0, 1])
def test_empty_diff_preflight_prevents_every_remote_mutation(
    tmp_path, empty_index
):
    integration, remote = adapter(tmp_path)
    candidates = entries(integration, ("core", "cli"))
    remote.empty_diffs.add(candidates[empty_index]["managed"])
    with pytest.raises(GitHubFailure) as failure:
        integration.publish(work(), candidates)
    assert failure.value.code == "github.no_changes"
    assert failure.value.uncertain is False
    assert not any(call[0] == "git" and "push" in call for call in remote.calls)
    assert not any(call[:3] == ["gh", "pr", "create"] for call in remote.calls)


def test_identical_base_candidate_is_a_no_change_result(tmp_path):
    integration, remote = adapter(tmp_path)
    candidate = entries(integration, ("core",))
    candidate[0]["base"] = candidate[0]["candidate"]
    remote.empty_diffs.add(candidate[0]["managed"])
    with pytest.raises(GitHubFailure) as failure:
        integration.publish(work(), candidate)
    assert failure.value.code == "github.no_changes"


def test_empty_diff_exact_owned_merged_pr_reconciles_without_republication(
    tmp_path,
):
    integration, remote = adapter(tmp_path)
    candidate = entries(integration, ("core",))
    original = integration.publish(work(), candidate)
    row = remote.prs[("owner/core", candidate[0]["branch"])][0]
    row.update(state="closed", merged_at="confirmed")
    candidate[0]["base"] = NEW
    remote.empty_diffs.add(candidate[0]["managed"])
    before = len(remote.calls)
    reconciled = integration.publish(work(), candidate)
    assert reconciled[0]["state"] == "merged"
    assert reconciled[0]["number"] == original[0]["number"]
    assert not any(
        call[0] == "git" and "push" in call for call in remote.calls[before:]
    )
    assert not any(
        call[:3] == ["gh", "pr", "create"] for call in remote.calls[before:]
    )
