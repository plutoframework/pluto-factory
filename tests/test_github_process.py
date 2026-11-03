"""Git credentials stay host-scoped and process errors expose no secrets."""

import pytest

from pluto_factory.github_process import (
    classify_failure,
    git_command,
    operation_name,
)


def command(operation="push"):
    return git_command(
        "/factory/repositories/snake",
        [
            operation,
            "https://github.com/owner/snake.git",
            "candidate:refs/heads/factory/task",
        ],
    )


def test_git_uses_scoped_gh_helper_after_reset_without_global_changes():
    argv = command()
    reset = argv.index("credential.helper=")
    scoped = argv.index(
        "credential.https://github.com.helper=!gh auth git-credential"
    )
    assert reset < scoped
    assert argv[reset - 1] == argv[scoped - 1] == "-c"
    assert "core.hooksPath=/dev/null" in argv
    assert not any(value in argv for value in ("--global", "--system", "token"))
    assert argv[argv.index("-C") + 1] == "/factory/repositories/snake"


@pytest.mark.parametrize(
    "diagnostic",
    [
        "fatal: could not read Username for 'https://github.com': terminal prompts disabled",
        "fatal: could not read Password for 'https://github.com': Device not configured",
        "fatal: Authentication failed for 'https://github.com/owner/snake.git/'",
    ],
)
def test_missing_git_credentials_are_authentication_failures(diagnostic):
    code, summary, uncertain = classify_failure(
        command(),
        128,
        diagnostic + "\nsentinel-credential",
        mutating=True,
    )
    assert code == "auth.required"
    assert not uncertain
    assert "Git push" in summary
    assert "sentinel" not in summary and "https://" not in summary


@pytest.mark.parametrize(
    "diagnostic",
    [
        " ! [remote rejected] candidate -> task (refusing to allow an OAuth App to create or update workflow validate.yml without workflow scope)",
        "remote: Permission to owner/snake.git denied to someone.",
        "fatal: unable to access GitHub: The requested URL returned error: 403",
        "GraphQL: Resource not accessible by integration",
        "HTTP 403: Resource not accessible by personal access token",
    ],
)
def test_repository_or_workflow_permissions_are_not_transport_ambiguity(
    diagnostic,
):
    code, summary, uncertain = classify_failure(
        command(),
        128,
        diagnostic + "\nsentinel-credential",
        mutating=True,
    )
    assert code == "permission.required"
    assert not uncertain
    assert "Git push" in summary
    assert "sentinel" not in summary


def test_gh_login_failure_is_safe_and_deterministic():
    result = classify_failure(
        ["gh", "pr", "create", "--body-file", "/private/body.md"],
        4,
        "sentinel-credential",
        mutating=True,
    )
    assert result == (
        "auth.required",
        "GitHub pr create requires GitHub authentication",
        False,
    )


@pytest.mark.parametrize("mutating", [True, False])
def test_transport_failure_preserves_uncertainty_and_safe_operation(mutating):
    result = classify_failure(
        command("push" if mutating else "ls-remote"),
        128,
        "fatal: unable to access GitHub: Recv failure: Connection reset by peer\nsentinel-credential",
        mutating=mutating,
    )
    assert result[0] == "github.operation_failed"
    assert result[2] is mutating
    assert "sentinel" not in result[1]


def test_unknown_command_arguments_are_never_reflected_in_error_summary():
    argv = git_command("/private/location", ["sentinel-credential"])
    assert operation_name(argv) == "Git operation"


def test_error_classifier_handles_missing_or_binary_stderr():
    assert classify_failure(command(), 128, None, mutating=True)[2]
    assert (
        classify_failure(
            command(),
            128,
            b"fatal: could not read Username for 'https://github.com': prompts disabled",
            mutating=True,
        )[0]
        == "auth.required"
    )


@pytest.mark.parametrize(
    "diagnostic",
    [
        "pull request create failed: GraphQL: No commits between main and factory/private-task (createPullRequest)",
        "GraphQL: No commits between private-main and private-candidate (createPullRequest)",
        "No commits between private-main and private-candidate",
        b"pull request create failed: GraphQL: No commits between main and private-candidate (createPullRequest)",
        '{"errors":[{"type":"UNPROCESSABLE","path":["createPullRequest"],"message":"No commits between private-main and private-candidate"}]}',
        '{"message":"Validation Failed","errors":[{"resource":"PullRequest","code":"custom","message":"No commits between private-main and private-candidate"}]}',
    ],
)
def test_empty_pr_creation_is_a_safe_deterministic_validation_rejection(
    diagnostic,
):
    result = classify_failure(
        [
            "gh",
            "pr",
            "create",
            "--head",
            "private-candidate",
            "--body-file",
            "/private/request.md",
        ],
        1,
        diagnostic,
        mutating=True,
    )
    assert result[0] == "github.no_changes"
    assert result[2] is False
    assert "reconcile" in result[1]
    assert "private" not in result[1] and "factory/" not in result[1]


@pytest.mark.parametrize(
    "argv,diagnostic",
    [
        (["git", "push"], "No commits between main and candidate"),
        (["gh", "issue", "create"], "No commits between main and candidate"),
        (["gh", "pr", "view"], "No commits between main and candidate"),
        (
            ["gh", "pr", "create"],
            'HTTP 502: upstream connection lost while handling "No commits between main and candidate"',
        ),
        (["gh", "pr", "create"], "Connection reset by peer\nunknown outcome"),
        (
            ["gh", "pr", "create"],
            '{"errors":[{"message":"No commits between main and candidate"}]}',
        ),
        (
            ["gh", "pr", "create"],
            '{"errors":[{"path":["otherMutation"],"message":"No commits between main and candidate"}]}',
        ),
        (
            ["gh", "pr", "create"],
            '{"errors": "No commits between main and candidate"}',
        ),
    ],
)
def test_empty_pr_classifier_does_not_hide_other_operation_or_transport_failures(
    argv, diagnostic
):
    result = classify_failure(argv, 1, diagnostic, mutating=True)
    assert result[0] == "github.operation_failed"
    assert result[2] is True
    assert "candidate" not in result[1]


@pytest.mark.parametrize(
    "returncode,diagnostic,expected",
    [
        (4, "No commits between main and candidate", "auth.required"),
        (
            1,
            "HTTP 401: Bad credentials\nNo commits between main and candidate",
            "auth.required",
        ),
        (
            1,
            "HTTP 403: Resource not accessible\nNo commits between main and candidate",
            "permission.required",
        ),
    ],
)
def test_auth_and_permission_classifications_keep_precedence(
    returncode, diagnostic, expected
):
    result = classify_failure(
        ["gh", "pr", "create"], returncode, diagnostic, mutating=True
    )
    assert result[0] == expected
    assert result[2] is False


def test_no_changes_summary_never_exposes_provider_diagnostics_or_refs():
    result = classify_failure(
        ["gh", "pr", "create", "--repo", "private-owner/private-repository"],
        1,
        "pull request create failed: GraphQL: No commits between private-base and private-head (createPullRequest)\nsentinel-token ghp_abcdefghijklmnop123456",
        mutating=True,
    )
    assert result[0] == "github.no_changes"
    assert not any(
        value in result[1]
        for value in (
            "private",
            "sentinel",
            "ghp_",
            "GraphQL",
            "createPullRequest",
        )
    )


def test_pr_adapter_propagates_typed_no_changes_without_runtime_side_effects(
    tmp_path,
):
    import subprocess

    from pluto_factory.github import GitHubAdapter, GitHubFailure
    from pluto_factory.models import FactoryConfig, Repository

    def rejected(argv, **_options):
        return subprocess.CompletedProcess(
            argv,
            1,
            "",
            "pull request create failed: GraphQL: No commits between private-base and private-head (createPullRequest)",
        )

    adapter = GitHubAdapter(
        tmp_path,
        FactoryConfig(
            name="example",
            repositories=[Repository(name="app", path=str(tmp_path / "repo"))],
        ),
        runner=rejected,
    )
    with pytest.raises(GitHubFailure) as error:
        adapter._run(["gh", "pr", "create"], mutating=True)
    assert error.value.code == "github.no_changes"
    assert error.value.uncertain is False
    assert "private" not in str(error.value)
    assert not list(tmp_path.iterdir())
