"""A deterministic, head-bound gate before requesting GitHub auto-merge."""

PASSING = {"SUCCESS"}
OPTIONAL_TERMINAL = {"NEUTRAL", "SKIPPED"}
FAILING = {
    "FAILURE",
    "ERROR",
    "TIMED_OUT",
    "ACTION_REQUIRED",
    "STARTUP_FAILURE",
    "CANCELLED",
    "STALE",
}
PENDING = {
    "PENDING",
    "QUEUED",
    "IN_PROGRESS",
    "WAITING",
    "REQUESTED",
    "EXPECTED",
}


def merge_check_status(pull_request: dict, required_names: list[str]) -> dict:
    """Only positive named results satisfy owner-declared required checks.

    Rollup rows are bound to observed_head by the PR query. Explicit head_sha
    metadata, when supplied, must agree too. Optional skipped/neutral checks
    are terminal, but cannot satisfy a named required check.
    """
    head = pull_request.get("head")
    observed = pull_request.get("observed_head")
    if (
        not head
        or not observed
        or pull_request.get("head_changed")
        or str(head).lower() != str(observed).lower()
    ):
        return {"ready": False, "reason": "head_changed"}
    if not isinstance(required_names, list) or any(
        not isinstance(name, str) or not name for name in required_names
    ):
        return {"ready": False, "reason": "invalid_required_checks"}
    rows = pull_request.get("checks", [])
    if not isinstance(rows, list):
        return {"ready": False, "reason": "invalid_check_rollup"}
    successful = set()
    pending = set()
    failed = set()
    incomplete = set()
    required = set(required_names)
    for index, check in enumerate(rows):
        if not isinstance(check, dict):
            incomplete.add(f"unrecognized-check-{index + 1}")
            continue
        name = check.get("name") or check.get("context")
        if not isinstance(name, str) or not name:
            name = f"unnamed-check-{index + 1}"
        explicit_head = check.get("head_sha") or check.get("headSha")
        if explicit_head and str(explicit_head).lower() != str(head).lower():
            incomplete.add(name)
            continue
        status = str(check.get("status") or "").upper()
        result = str(
            check.get("conclusion") or check.get("state") or ""
        ).upper()
        if status in PENDING or result in PENDING:
            pending.add(name)
        elif result in FAILING:
            failed.add(name)
        elif status and status != "COMPLETED":
            incomplete.add(name)
        elif result in PASSING:
            successful.add(name)
        elif result in OPTIONAL_TERMINAL:
            if name in required:
                incomplete.add(name)
        else:
            incomplete.add(name)
    missing = required - successful
    ready = not (missing or pending or failed or incomplete)
    return {
        "ready": ready,
        "reason": "checks_passed" if ready else "checks_not_ready",
        "head": head,
        "required": sorted(required),
        "successful": sorted(successful),
        "missing": sorted(missing),
        "pending": sorted(pending),
        "failed": sorted(failed),
        "incomplete": sorted(incomplete),
    }
