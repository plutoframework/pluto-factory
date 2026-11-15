"""Current evidence and explicit product acceptance for integration intent."""

import json

from .config import digest, resolved_bundle


def current_evidence(state, home, config, work, config_digest=None):
    candidates = {
        entry["name"]: entry["candidate"] for entry in work["data"]["entries"]
    }
    current_digest = config_digest or digest(resolved_bundle(home, config))
    rows = state.db.execute(
        "SELECT id,data FROM evidence WHERE work_id=?", (work["id"],)
    ).fetchall()
    results = []
    for row in rows:
        result = json.loads(row["data"])
        if result.get("kind") in {
            "github_ci",
            "github_delivery",
            "github_default_ci",
        } and (
            result.get("observation_fingerprint")
            != work["data"].get("post_merge_validation", {}).get("fingerprint")
            or row["id"]
            not in work["data"]
            .get("post_merge_validation", {})
            .get("evidence_ids", [])
        ):
            continue
        if (
            result.get("candidate_set") == candidates
            and result.get("config_digest") == current_digest
            and result.get("passed")
        ):
            results.append(
                {
                    "id": row["id"],
                    "citable": result.get("kind") not in NON_CITABLE_KINDS,
                    **result,
                }
            )
    return results


# Worker-side snapshot checks are advisory feedback for the implementer; only
# factory-run validation, reviewer and QA evidence can back a criterion.
NON_CITABLE_KINDS = {"worker_check"}


def citable_ids(items):
    return {item["id"] for item in items if item.get("citable", True)}


class AcceptanceContractError(ValueError):
    """Invalid acceptance assessment; detail is precise enough to retry on."""

    def __init__(self, problems, citable):
        self.problems = problems
        self.citable = sorted(citable)
        super().__init__(
            "acceptance contract invalid: "
            + "; ".join(problems)
            + ". Citable current evidence ids: "
            + (", ".join(self.citable) or "none")
        )


def acceptance_intent(decision, count, evidence_ids):
    """Only assessed incomplete product criteria justify another increment.

    Worker to-do prose is advisory. Coordinator-owned publication and merge
    steps are never evidence of unfinished product scope. A criterion the
    configured checks cannot verify is ``unverifiable``: it keeps the work from
    completing but permits publishing a safe increment. ``waived`` records an
    authoritative human decision and needs no evidence.
    """
    if decision.action not in {"advance", "complete"}:
        raise ValueError("acceptance intent requires a publication decision")
    assessments = {item.criterion: item for item in decision.acceptance}
    if set(assessments) != set(range(count)) or len(assessments) != len(
        decision.acceptance
    ):
        raise ValueError("integration lacks a complete acceptance assessment")
    remaining = []
    problems = []
    for assessment in decision.acceptance:
        if assessment.result == "failed":
            raise ValueError(
                "failed acceptance requires revision before integration"
            )
        number = assessment.criterion
        bad = [r for r in assessment.evidence_refs if r not in evidence_ids]
        if bad:
            problems.append(
                f"criterion {number} cites evidence that is absent, stale or "
                f"not citable: {', '.join(bad)}"
            )
        if assessment.result == "passed" and not assessment.evidence_refs:
            problems.append(
                f"criterion {number} is 'passed' without evidence_refs; cite "
                "citable evidence, or use result 'waived' for an owner "
                "decision, or 'unverifiable'/'incomplete' if no check proves it"
            )
        if assessment.result in {"incomplete", "unverifiable"}:
            remaining.append(assessment.explanation)
    if problems:
        raise AcceptanceContractError(problems, evidence_ids)
    if decision.action == "complete" and remaining:
        raise ValueError("completion contains incomplete acceptance criteria")
    action = "advance" if remaining else "complete"
    return decision.model_copy(update={"action": action}), remaining
