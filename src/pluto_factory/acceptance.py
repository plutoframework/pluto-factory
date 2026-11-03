"""Current evidence and explicit product acceptance for integration intent."""

import json

from .config import digest, resolved_bundle


def current_evidence(state, home, config, work):
    candidates = {
        entry["name"]: entry["candidate"] for entry in work["data"]["entries"]
    }
    current_digest = digest(resolved_bundle(home, config))
    rows = state.db.execute(
        "SELECT id,data FROM evidence WHERE work_id=?", (work["id"],)
    ).fetchall()
    results = []
    for row in rows:
        result = json.loads(row["data"])
        if (
            result.get("candidate_set") == candidates
            and result.get("config_digest") == current_digest
            and result.get("passed")
        ):
            results.append({"id": row["id"], **result})
    return results


def acceptance_intent(decision, count, evidence_ids):
    """Only assessed incomplete product criteria justify another increment.

    Worker to-do prose is advisory. Coordinator-owned publication and merge
    steps are never evidence of unfinished product scope.
    """
    if decision.action not in {"advance", "complete"}:
        raise ValueError("acceptance intent requires a publication decision")
    assessments = {item.criterion: item for item in decision.acceptance}
    if set(assessments) != set(range(count)) or len(assessments) != len(
        decision.acceptance
    ):
        raise ValueError("integration lacks a complete acceptance assessment")
    remaining = []
    for assessment in decision.acceptance:
        if assessment.result == "failed":
            raise ValueError(
                "failed acceptance requires revision before integration"
            )
        if not set(assessment.evidence_refs).issubset(evidence_ids) or (
            assessment.result == "passed" and not assessment.evidence_refs
        ):
            raise ValueError("acceptance cites absent or stale evidence")
        if assessment.result == "incomplete":
            remaining.append(assessment.explanation)
    if decision.action == "complete" and remaining:
        raise ValueError("completion contains incomplete acceptance criteria")
    action = "advance" if remaining else "complete"
    return decision.model_copy(update={"action": action}), remaining
