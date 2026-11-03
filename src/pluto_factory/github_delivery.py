"""Collect current default-branch workflow jobs at an exact PR merge commit.

Only run identity/job metadata is requested: no logs, credentials or PR-head CI.
Provider failures retain their typed GitHubFailure for coordinator recovery.
"""

import re
from datetime import datetime
from urllib.parse import urlsplit

SHA = re.compile(r"(?:[a-fA-F0-9]{40}|[a-fA-F0-9]{64})")
EVENTS = {"push", "workflow_dispatch", "schedule"}
STATUSES = {
    "queued",
    "in_progress",
    "completed",
    "waiting",
    "requested",
    "pending",
}
CONCLUSIONS = {
    "success",
    "failure",
    "neutral",
    "cancelled",
    "skipped",
    "timed_out",
    "action_required",
    "stale",
    "startup_failure",
    "error",
}
FIELDS = "databaseId,workflowDatabaseId,workflowName,headSha,headBranch,event,status,conclusion,url,createdAt,updatedAt,attempt"


class _InvalidObservation(Exception):
    pass


def _positive(value):
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise _InvalidObservation
    return value


def _name(value):
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > 500
        or any(not c.isprintable() for c in value)
    ):
        raise _InvalidObservation
    return value


def _timestamp(value):
    if not isinstance(value, str):
        raise _InvalidObservation
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise _InvalidObservation from error
    if parsed.tzinfo is None:
        raise _InvalidObservation
    return value, parsed.timestamp()


def _status(row):
    status, conclusion = row.get("status"), row.get("conclusion") or ""
    if (
        not isinstance(status, str)
        or not isinstance(conclusion, str)
        or status not in STATUSES
        or conclusion not in CONCLUSIONS | {""}
    ):
        raise _InvalidObservation
    if status == "completed" and not conclusion:
        raise _InvalidObservation
    return status.upper(), conclusion.upper()


def _workflow(row):
    workflow_id = row.get("workflowDatabaseId")
    name = row.get("workflowName")
    if workflow_id is not None:
        workflow_id = _positive(workflow_id)
        if name is not None and name != "":
            name = _name(name)
        return ("id", workflow_id), workflow_id, name or ""
    return ("name", _name(name)), None, name


def _url(value, slug, run_id, job_id=None, attempt=None):
    if not isinstance(value, str):
        raise _InvalidObservation
    try:
        parsed = urlsplit(value)
    except ValueError as error:
        raise _InvalidObservation from error
    if (
        parsed.scheme != "https"
        or parsed.netloc.lower() != "github.com"
        or parsed.fragment
    ):
        raise _InvalidObservation
    base = f"/{slug}/actions/runs/{run_id}"
    paths = (
        {base}
        if job_id is None
        else {f"{base}/job/{job_id}", f"/{slug}/runs/{job_id}"}
    )
    if job_id is None and attempt:
        paths.add(f"{base}/attempts/{attempt}")
    legacy_job = (
        job_id is not None
        and parsed.path.lower() == f"/{slug}/runs/{job_id}".lower()
    )
    if parsed.path.lower() not in {path.lower() for path in paths} or (
        parsed.query
        and not (legacy_job and parsed.query == "check_suite_focus=true")
    ):
        raise _InvalidObservation
    return f"https://github.com{base}" + (f"/job/{job_id}" if job_id else "")


def _run(row, slug, branch, head):
    if not isinstance(row, dict):
        raise _InvalidObservation
    run_id = _positive(row.get("databaseId"))
    attempt = _positive(row.get("attempt"))
    if (
        row.get("headSha") != head
        or row.get("headBranch") != branch
        or not isinstance(row.get("event"), str)
        or row.get("event") not in EVENTS
    ):
        raise _InvalidObservation
    key, workflow_id, workflow_name = _workflow(row)
    created_at, created = _timestamp(row.get("createdAt"))
    updated_at, updated = _timestamp(row.get("updatedAt"))
    status, conclusion = _status(row)
    return (
        key,
        (run_id, created, attempt, updated),
        {
            "id": run_id,
            "head_sha": head,
            "head_branch": branch,
            "event": row["event"],
            "status": status,
            "conclusion": conclusion,
            "url": _url(row.get("url"), slug, run_id, attempt=attempt),
            "updated_at": updated_at,
            "created_at": created_at,
            "workflow_id": workflow_id,
            "workflow_name": workflow_name,
            "attempt": attempt,
        },
    )


def _checks(row, run, slug):
    jobs = row.get("jobs")
    if not isinstance(jobs, list):
        raise _InvalidObservation
    checks, seen = [], set()
    common = {
        "head_sha": run["head_sha"],
        "run_id": run["id"],
        "run_attempt": run["attempt"],
        "workflow_id": run["workflow_id"],
        "workflow_name": run["workflow_name"],
    }
    for job in jobs:
        if not isinstance(job, dict):
            raise _InvalidObservation
        job_id = _positive(job.get("databaseId"))
        if job_id in seen:
            raise _InvalidObservation
        seen.add(job_id)
        status, conclusion = _status(job)
        checks.append(
            {
                **common,
                "name": _name(job.get("name")),
                "status": status,
                "conclusion": conclusion,
                "job_id": job_id,
                "url": _url(job.get("url"), slug, run["id"], job_id),
                "started_at": _timestamp(job["startedAt"])[0]
                if job.get("startedAt")
                else None,
                "completed_at": _timestamp(job["completedAt"])[0]
                if job.get("completedAt")
                else None,
            }
        )
    # Pending workflows can have no jobs yet or only finished partial jobs.
    # A run-level failure can also occur before any job is created.
    if run["status"] != "COMPLETED" or run["conclusion"] not in {
        "SUCCESS",
        "NEUTRAL",
        "SKIPPED",
    }:
        checks.append(
            {
                **common,
                "name": "workflow:"
                + (run["workflow_name"] or str(run["workflow_id"])),
                "status": run["status"],
                "conclusion": run["conclusion"],
                "job_id": None,
                "url": run["url"],
                "source": "workflow_run",
            }
        )
    return checks


def default_branch_checks(adapter, pull_request):
    slug, _number = adapter._pr_scope(pull_request)
    branch, head = pull_request.get("base"), pull_request.get("merge_commit")
    result = {
        "state": "unavailable",
        "branch": branch if isinstance(branch, str) else None,
        "head": head if isinstance(head, str) and SHA.fullmatch(head) else None,
        "checks": [],
        "runs": [],
    }
    if head is None or head == "":
        return result | {"summary": "Merged commit identity is unavailable"}
    if (
        not isinstance(head, str)
        or not SHA.fullmatch(head)
        or not isinstance(branch, str)
        or not branch
        or branch.startswith("-")
        or any(c.isspace() or not c.isprintable() for c in branch)
    ):
        return result | {
            "state": "invalid",
            "summary": "Default-branch CI identity is invalid",
        }
    head = head.lower()
    result["head"] = head
    try:
        rows = adapter._json(
            [
                "gh",
                "run",
                "list",
                "--repo",
                slug,
                "--branch",
                branch,
                "--commit",
                head,
                "--limit",
                "100",
                "--json",
                FIELDS,
            ]
        )
        if not isinstance(rows, list):
            raise _InvalidObservation
        latest = {}
        for row in rows:
            if not isinstance(row, dict):
                raise _InvalidObservation
            if row.get("headSha") != head or row.get("headBranch") != branch:
                continue
            if not isinstance(row.get("event"), str):
                raise _InvalidObservation
            if row["event"] not in EVENTS:
                continue
            key, rank, run = _run(row, slug, branch, head)
            previous = latest.get(key)
            if previous is None or rank > previous[0]:
                latest[key] = rank, run
        if not latest:
            return result | {
                "state": "not_found",
                "summary": "No matching default-branch workflow runs were observed",
            }
        checks, runs = [], []
        for key, (rank, listed) in sorted(latest.items()):
            row = adapter._json(
                [
                    "gh",
                    "run",
                    "view",
                    str(listed["id"]),
                    "--repo",
                    slug,
                    "--json",
                    FIELDS + ",jobs",
                ]
            )
            view_key, view_rank, run = _run(row, slug, branch, head)
            if (
                view_key != key
                or run["id"] != listed["id"]
                or run["event"] != listed["event"]
                or run["attempt"] < listed["attempt"]
                or view_rank[3] < rank[3]
            ):
                raise _InvalidObservation
            checks.extend(_checks(row, run, slug))
            runs.append(run)
        return result | {"state": "observed", "checks": checks, "runs": runs}
    except _InvalidObservation:
        return result | {
            "state": "invalid",
            "summary": "Default-branch workflow metadata could not be verified",
        }
