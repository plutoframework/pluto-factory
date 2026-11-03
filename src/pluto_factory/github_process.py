"""GitHub-scoped credentials and safe process failure classification."""

import json
import re


def git_command(managed, arguments):
    return [
        "git",
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        "core.fsmonitor=false",
        "-c",
        "commit.gpgsign=false",
        "-c",
        "credential.helper=",
        "-c",
        "credential.https://github.com.helper=!gh auth git-credential",
        "-C",
        str(managed),
        *arguments,
    ]


def operation_name(argv):
    if argv and argv[0] == "git" and "-C" in argv:
        position = argv.index("-C") + 2
        operation = argv[position] if position < len(argv) else ""
        if operation in {
            "fetch",
            "push",
            "ls-remote",
            "merge",
            "merge-base",
            "rev-parse",
            "branch",
            "status",
            "check-ref-format",
        }:
            return f"Git {operation}"
        return "Git operation"
    if argv and argv[0] == "gh":
        groups = {"api", "repo", "pr", "issue"}
        verbs = {"view", "create", "edit", "merge", "close"}
        if len(argv) > 1 and argv[1] in groups:
            group = argv[1]
            if len(argv) > 2 and argv[2] in verbs:
                return f"GitHub {group} {argv[2]}"
            return f"GitHub {group}"
    return "GitHub integration operation"


def _no_changes(diagnostic):
    """Recognize a PR-creation validation rejection, never a transport hint."""
    message = re.compile(
        r"no commits between \S[^\r\n]* and \S[^\r\n]*", re.IGNORECASE
    )
    for line in diagnostic.splitlines():
        line = re.sub(
            r"^(?:pull request create failed:\s*)?(?:GraphQL:\s*)?",
            "",
            line.strip(),
            flags=re.IGNORECASE,
        )
        if message.fullmatch(line):
            return True
    try:
        response = json.loads(diagnostic)
    except (ValueError, TypeError):
        return False
    if not isinstance(response, dict) or not isinstance(
        response.get("errors"), list
    ):
        return False
    for error in response["errors"]:
        if not isinstance(error, dict) or not isinstance(
            error.get("message"), str
        ):
            continue
        path = error.get("path")
        creation_path = isinstance(path, list) and any(
            isinstance(value, str) and value.lower() == "createpullrequest"
            for value in path
        )
        pull_request_resource = error.get("resource") == "PullRequest"
        if (creation_path or pull_request_resource) and message.fullmatch(
            error["message"].strip()
        ):
            return True
    return False


def classify_failure(argv, returncode, stderr, *, mutating):
    operation = operation_name(argv)
    if isinstance(stderr, bytes):
        stderr = stderr.decode(errors="replace")
    diagnostic = str(stderr or "").lower()
    git_auth = bool(
        re.search(
            r"(?m)^fatal: (?:could not read (?:username|password) for "
            r"['\"]https://github\.com|authentication failed for "
            r"['\"]https://github\.com)",
            diagnostic,
        )
    )
    gh_auth = (
        bool(argv and argv[0] == "gh" and returncode == 4)
        or "http 401" in diagnostic
        or "not logged into any github hosts" in diagnostic
    )
    if git_auth or gh_auth:
        return (
            "auth.required",
            f"{operation} requires GitHub authentication",
            False,
        )
    workflow_permission = (
        "refusing to allow" in diagnostic
        and "create or update workflow" in diagnostic
        and "without" in diagnostic
    )
    denied = (
        workflow_permission
        or "http 403" in diagnostic
        or "requested url returned error: 403" in diagnostic
        or "resource not accessible by integration" in diagnostic
        or "resource not accessible by personal access token" in diagnostic
        or bool(
            re.search(r"(?m)^remote: permission to .+ denied to ", diagnostic)
        )
        or bool(
            re.search(
                r"(?m)^remote: write access to repository not granted",
                diagnostic,
            )
        )
    )
    if denied:
        return (
            "permission.required",
            f"{operation} requires GitHub permission",
            False,
        )
    if (
        argv
        and list(argv[:3]) == ["gh", "pr", "create"]
        and returncode != 0
        and _no_changes(str(stderr or ""))
    ):
        return (
            "github.no_changes",
            (
                "GitHub PR creation has no commits to publish; reconcile the "
                "candidate with the target branch before retrying"
            ),
            False,
        )
    return (
        "github.operation_failed",
        f"{operation} failed (exit {returncode})",
        mutating,
    )
