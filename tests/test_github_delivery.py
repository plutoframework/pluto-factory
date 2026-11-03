"""Default-branch delivery observations are merge-bound, current and read-only."""

import copy

import pytest

from pluto_factory.github import GitHubAdapter, GitHubFailure
from pluto_factory.github_checks import merge_check_status
from pluto_factory.models import FactoryConfig, Repository

SHA = "a" * 40
PR_HEAD = "b" * 40
SLUG = "owner/repo"


def run(run_id=101, *, workflow_id=11, workflow="Validate", **changes):
    return {
        "databaseId": run_id,
        "workflowDatabaseId": workflow_id,
        "workflowName": workflow,
        "headSha": SHA,
        "headBranch": "main",
        "event": "push",
        "status": "completed",
        "conclusion": "success",
        "url": f"https://github.com/{SLUG}/actions/runs/{run_id}",
        "createdAt": "2026-11-03T07:00:00Z",
        "updatedAt": "2026-11-03T07:05:00Z",
        "attempt": 1,
        **changes,
    }


def job(run_id=101, job_id=201, *, name="validate", **changes):
    return {
        "databaseId": job_id,
        "name": name,
        "status": "completed",
        "conclusion": "success",
        "url": f"https://github.com/{SLUG}/actions/runs/{run_id}/job/{job_id}",
        "startedAt": "2026-11-03T07:01:00Z",
        "completedAt": "2026-11-03T07:05:00Z",
        "steps": [{"name": "PRIVATE STEP MUST NOT BE EXPOSED"}],
        **changes,
    }


def adapter(tmp_path, rows, views=None):
    config = FactoryConfig(
        name="delivery",
        repositories=[Repository(name="app", path=str(tmp_path), github=SLUG)],
    )
    instance = GitHubAdapter(tmp_path, config)
    calls = []

    def response(argv, **options):
        calls.append((argv, options))
        assert options == {}
        assert argv[:2] == ["gh", "run"]
        if argv[2] == "list":
            return copy.deepcopy(rows)
        assert argv[2] == "view"
        return copy.deepcopy(views[int(argv[3])])

    instance._json = response
    return instance, calls


def pull_request(**changes):
    return {
        "repository": "app",
        "slug": SLUG,
        "number": 17,
        "base": "main",
        "head": PR_HEAD,
        "merge_commit": SHA,
        **changes,
    }


def gate(observation):
    return merge_check_status(
        {
            "head": SHA,
            "observed_head": SHA,
            "head_changed": False,
            "checks": observation["checks"],
        },
        ["validate"],
    )


def test_authentic_gh_fields_produce_only_exact_merge_commit_jobs(tmp_path):
    listed = run()
    instance, calls = adapter(
        tmp_path, [listed], {101: {**listed, "jobs": [job()]}}
    )
    result = instance.default_branch_checks(pull_request())
    assert result["state"] == "observed" and result["head"] == SHA
    assert result["branch"] == "main"
    assert result["checks"][0]["head_sha"] == SHA != PR_HEAD
    assert result["checks"][0]["run_id"] == 101
    assert result["checks"][0]["job_id"] == 201
    assert result["checks"][0]["run_attempt"] == 1
    assert result["runs"][0]["workflow_id"] == 11
    assert gate(result)["ready"]
    assert "PRIVATE STEP" not in str(result)
    assert calls[0][0][:11] == [
        "gh",
        "run",
        "list",
        "--repo",
        SLUG,
        "--branch",
        "main",
        "--commit",
        SHA,
        "--limit",
        "100",
    ]
    assert calls[1][0][:6] == ["gh", "run", "view", "101", "--repo", SLUG]
    assert "--log" not in str(calls) and "--log-failed" not in str(calls)
    supported = {
        "databaseId",
        "workflowDatabaseId",
        "workflowName",
        "headSha",
        "headBranch",
        "event",
        "status",
        "conclusion",
        "url",
        "createdAt",
        "updatedAt",
        "attempt",
        "jobs",
    }
    for argv, _options in calls:
        assert set(argv[argv.index("--json") + 1].split(",")).issubset(
            supported
        )


@pytest.mark.parametrize(
    "changes",
    [
        {"headSha": PR_HEAD},
        {"headBranch": "feature"},
        {"event": "pull_request"},
        {"event": "pull_request_target"},
        {"event": "merge_group"},
    ],
)
def test_list_never_accepts_other_sha_branch_or_pr_event(tmp_path, changes):
    instance, calls = adapter(tmp_path, [run(**changes)])
    assert (
        instance.default_branch_checks(pull_request())["state"] == "not_found"
    )
    assert len(calls) == 1


@pytest.mark.parametrize("event", ["push", "workflow_dispatch", "schedule"])
def test_supported_default_branch_events_are_bound_to_exact_merge(
    tmp_path, event
):
    listed = run(event=event)
    instance, _ = adapter(
        tmp_path, [listed], {101: {**listed, "jobs": [job()]}}
    )
    assert instance.default_branch_checks(pull_request())["state"] == "observed"


@pytest.mark.parametrize(
    "changes",
    [
        {"databaseId": 102},
        {"headSha": PR_HEAD},
        {"headBranch": "feature"},
        {"event": "pull_request"},
        {"event": "schedule"},
        {"workflowDatabaseId": 12},
        {"attempt": 0},
        {"attempt": True},
        {"updatedAt": "2026-11-03T07:04:00Z"},
    ],
)
def test_view_revalidates_selected_run_identity_instead_of_trusting_list(
    tmp_path, changes
):
    listed = run()
    instance, _ = adapter(
        tmp_path, [listed], {101: {**listed, "jobs": [job()], **changes}}
    )
    result = instance.default_branch_checks(pull_request())
    assert result["state"] == "invalid"
    assert result["checks"] == [] and result["runs"] == []


def test_newest_workflow_run_blocks_older_success_even_when_old_run_updated_later(
    tmp_path,
):
    old = run(updatedAt="2026-11-03T07:20:00Z")
    new = run(
        102,
        createdAt="2026-11-03T07:10:00Z",
        updatedAt="2026-11-03T07:10:00Z",
        status="queued",
        conclusion="",
    )
    instance, calls = adapter(tmp_path, [old, new], {102: {**new, "jobs": []}})
    result = instance.default_branch_checks(pull_request())
    assert result["runs"][0]["id"] == 102
    assert not gate(result)["ready"]
    assert result["checks"][0]["status"] == "QUEUED"
    assert all("101" not in argv for argv, _ in calls)


def test_latest_attempt_and_current_job_identity_change_observation(tmp_path):
    old = run()
    new = run(
        attempt=2,
        status="in_progress",
        conclusion="",
        updatedAt="2026-11-03T07:10:00Z",
    )
    instance, _ = adapter(
        tmp_path,
        [old, new],
        {
            101: {
                **new,
                "jobs": [job(job_id=202, status="in_progress", conclusion="")],
            }
        },
    )
    result = instance.default_branch_checks(pull_request())
    assert result["runs"][0]["attempt"] == 2
    assert result["checks"][0]["job_id"] == 202
    assert result["checks"][0]["run_attempt"] == 2
    assert not gate(result)["ready"]


def test_rerun_started_between_list_and_view_uses_new_current_attempt(tmp_path):
    listed, current = (
        run(),
        run(
            attempt=2,
            status="queued",
            conclusion="",
            updatedAt="2026-11-03T07:10:00Z",
        ),
    )
    instance, _ = adapter(tmp_path, [listed], {101: {**current, "jobs": []}})
    result = instance.default_branch_checks(pull_request())
    assert result["state"] == "observed"
    assert result["runs"][0]["attempt"] == 2
    assert not gate(result)["ready"]


@pytest.mark.parametrize(
    "status,conclusion",
    [("queued", ""), ("in_progress", ""), ("completed", "failure")],
)
def test_non_success_workflow_remains_explicit_even_with_no_jobs(
    tmp_path, status, conclusion
):
    listed = run(status=status, conclusion=conclusion)
    instance, _ = adapter(tmp_path, [listed], {101: {**listed, "jobs": []}})
    result = instance.default_branch_checks(pull_request())
    assert result["state"] == "observed" and result["checks"]
    assert result["checks"][0]["source"] == "workflow_run"
    assert not gate(result)["ready"]


def test_finished_success_without_jobs_never_fabricates_successful_check(
    tmp_path,
):
    listed = run()
    instance, _ = adapter(tmp_path, [listed], {101: {**listed, "jobs": []}})
    result = instance.default_branch_checks(pull_request())
    assert result["state"] == "observed" and result["runs"]
    assert result["checks"] == []
    assert not gate(result)["ready"]


def test_finished_partial_jobs_do_not_make_in_progress_workflow_green(tmp_path):
    listed = run(status="in_progress", conclusion="")
    instance, _ = adapter(
        tmp_path, [listed], {101: {**listed, "jobs": [job()]}}
    )
    result = instance.default_branch_checks(pull_request())
    assert len(result["checks"]) == 2
    assert not gate(result)["ready"]


def test_latest_runs_are_selected_independently_per_workflow(tmp_path):
    validate, security = run(), run(102, workflow_id=12, workflow="Security")
    instance, _ = adapter(
        tmp_path,
        [validate, security],
        {
            101: {**validate, "jobs": [job()]},
            102: {
                **security,
                "jobs": [job(102, 202, name="security", conclusion="failure")],
            },
        },
    )
    result = instance.default_branch_checks(pull_request())
    assert len(result["runs"]) == 2
    assert not gate(result)["ready"]


@pytest.mark.parametrize(
    "changes",
    [
        {"workflowDatabaseId": None, "workflowName": None},
        {"workflowDatabaseId": "11"},
        {"workflowDatabaseId": 0},
        {"workflowDatabaseId": True},
        {"workflowName": "", "workflowDatabaseId": None},
        {"databaseId": "101"},
    ],
)
def test_unverifiable_workflow_or_run_identity_is_invalid(tmp_path, changes):
    instance, _ = adapter(tmp_path, [run(**changes)])
    assert instance.default_branch_checks(pull_request())["state"] == "invalid"


def test_missing_workflow_id_can_use_exact_nonempty_workflow_name(tmp_path):
    listed = run(workflowDatabaseId=None)
    instance, _ = adapter(
        tmp_path, [listed], {101: {**listed, "jobs": [job()]}}
    )
    assert gate(instance.default_branch_checks(pull_request()))["ready"]


@pytest.mark.parametrize(
    "changes",
    [
        {"databaseId": None},
        {"url": "https://github.com/owner/repo/actions/runs/999/job/201"},
        {"url": "https://evil.example/job/201"},
        {
            "url": "https://github.com/owner/repo/actions/runs/101/job/201?token=sentinel"
        },
        {"status": "unknown"},
        {"conclusion": ""},
    ],
)
def test_invalid_job_metadata_cannot_become_evidence(tmp_path, changes):
    listed = run()
    instance, _ = adapter(
        tmp_path, [listed], {101: {**listed, "jobs": [job(**changes)]}}
    )
    result = instance.default_branch_checks(pull_request())
    assert result["state"] == "invalid" and result["checks"] == []
    assert "sentinel" not in result["summary"]


def test_legacy_github_job_url_is_verified_and_normalized(tmp_path):
    listed = run()
    instance, _ = adapter(
        tmp_path,
        [listed],
        {
            101: {
                **listed,
                "jobs": [
                    job(
                        url="https://github.com/owner/repo/runs/201?check_suite_focus=true"
                    )
                ],
            }
        },
    )
    result = instance.default_branch_checks(pull_request())
    assert result["state"] == "observed"
    assert (
        result["checks"][0]["url"]
        == "https://github.com/owner/repo/actions/runs/101/job/201"
    )


def test_absent_merge_identity_is_unavailable_without_process_calls(tmp_path):
    instance, calls = adapter(tmp_path, [])
    assert (
        instance.default_branch_checks(pull_request(merge_commit=None))["state"]
        == "unavailable"
    )
    assert not calls


@pytest.mark.parametrize(
    "merge_commit", ["short", "a" * 41, "ghp_sentinel", {}, True]
)
def test_invalid_merge_identity_cannot_trigger_process_calls(
    tmp_path, merge_commit
):
    instance, calls = adapter(tmp_path, [])
    result = instance.default_branch_checks(
        pull_request(merge_commit=merge_commit)
    )
    assert result["state"] == "invalid"
    assert not calls
    assert "sentinel" not in result["summary"]


@pytest.mark.parametrize(
    "code", ["auth.required", "permission.required", "github.operation_failed"]
)
def test_provider_failure_keeps_typed_recovery_signal_and_no_diagnostic_output(
    tmp_path, code
):
    instance, _ = adapter(tmp_path, [])

    def fail(*_arguments, **_options):
        raise GitHubFailure(
            code,
            "Fixed safe provider summary",
            code == "github.operation_failed",
        )

    instance._json = fail
    with pytest.raises(GitHubFailure) as error:
        instance.default_branch_checks(pull_request())
    assert error.value.code == code
    assert error.value.uncertain is (code == "github.operation_failed")


def test_pull_request_scope_violation_is_rejected_before_api_calls(tmp_path):
    instance, calls = adapter(tmp_path, [])
    with pytest.raises(GitHubFailure) as error:
        instance.default_branch_checks(pull_request(slug="other/repo"))
    assert error.value.code == "scope.denied" and not calls


def test_poll_records_actual_merge_commit_and_time_from_gh_metadata(tmp_path):
    instance, _ = adapter(tmp_path, [])
    captured = []

    def response(argv, **_options):
        captured.append(argv)
        return {
            "headRefName": "factory/task",
            "baseRefName": "main",
            "headRefOid": PR_HEAD,
            "state": "MERGED",
            "mergedAt": "2026-11-03T07:00:00Z",
            "mergeCommit": {"oid": SHA},
        }

    instance._json = response
    result = instance.poll_pull_requests([pull_request(branch="factory/task")])
    record = result["pull_requests"][0]
    assert (
        record["merge_commit"] == SHA
        and record["merged_at"] == "2026-11-03T07:00:00Z"
    )
    assert "mergeCommit" in captured[0][-1].split(",")


@pytest.mark.parametrize(
    "changes",
    [
        {"status": {}},
        {"conclusion": {"private": "sentinel"}},
        {"event": {}},
        {"headSha": None},
        {"url": "https://[invalid"},
    ],
)
def test_malformed_view_values_fail_closed_without_exception_or_raw_data(
    tmp_path, changes
):
    listed = run()
    instance, _ = adapter(
        tmp_path, [listed], {101: {**listed, "jobs": [job()], **changes}}
    )
    result = instance.default_branch_checks(pull_request())
    assert result["state"] == "invalid"
    assert result["checks"] == [] and result["runs"] == []
    assert "sentinel" not in str(result)


def test_invalid_merge_identity_does_not_echo_unverified_secret_like_value(
    tmp_path,
):
    instance, _ = adapter(tmp_path, [])
    result = instance.default_branch_checks(
        pull_request(merge_commit="ghp_sentinel")
    )
    assert result["state"] == "invalid" and result["head"] is None
    assert "sentinel" not in str(result)


def test_github_repository_url_casing_is_not_treated_as_another_identity(
    tmp_path,
):
    listed = run(url="https://github.com/Owner/Repo/actions/runs/101")
    instance, _ = adapter(
        tmp_path,
        [listed],
        {
            101: {
                **listed,
                "jobs": [
                    job(
                        url="https://github.com/Owner/Repo/actions/runs/101/job/201"
                    )
                ],
            }
        },
    )
    assert gate(instance.default_branch_checks(pull_request()))["ready"]
