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
