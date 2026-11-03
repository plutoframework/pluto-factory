"""Delivery gates and fresh acceptance after confirmed GitHub merges."""

import time

from .acceptance import acceptance_intent, current_evidence
from .config import digest, resolved_bundle
from .coordinator import CoordinatorError
from .github_checks import merge_check_status
from .models import Failure


def observation(engine, work):
    data = work["data"]
    gates = [
        merge_check_status(pr, engine.config.github_required_checks)
        for pr in data["pull_requests"]
    ]
    fingerprint = digest(
        {
            "candidates": data["acceptance_candidate"],
            "config_digest": digest(
                resolved_bundle(engine.home, engine.config)
            ),
            "pull_requests": [
                {
                    k: pr.get(k)
                    for k in (
                        "repository",
                        "slug",
                        "number",
                        "head",
                        "observed_head",
                        "checks",
                    )
                }
                for pr in data["pull_requests"]
            ],
            "required_checks": engine.config.github_required_checks,
        }
    )
    return gates, fingerprint


def wait_for_checks(engine, work, fingerprint, summary):
    data = work["data"]
    data.pop("post_merge_acceptance", None)
    data["post_merge_validation"] = {
        "fingerprint": fingerprint,
        "status": "waiting",
        "evidence_ids": [],
    }
    engine.state.update_work(
        work["id"],
        data=data,
        control="backoff",
        wake_at=time.time() + engine.config.github_poll_seconds,
        event="validation.remote_ci.waiting",
    )
    engine.state.notify(
        work["id"],
        "progress",
        summary,
        "post-merge-wait:" + work["id"] + ":" + fingerprint,
    )


def reject_observation(engine, work, fingerprint, reason):
    data = work["data"]
    data.pop("post_merge_acceptance", None)
    data["post_merge_validation"] = {
        "fingerprint": fingerprint,
        "status": "failed",
        "evidence_ids": [],
    }
    work = engine.state.update_work(
        work["id"], data=data, event="validation.remote_ci.failed"
    )
    engine._defer(work, reason)


def gate_checks(engine, work, gates, fingerprint):
    if any(gate.get("reason") == "head_changed" for gate in gates):
        reject_observation(
            engine,
            work,
            fingerprint,
            "Merged PR head changed; revalidation is required",
        )
        return False
    if any(gate.get("failed") for gate in gates):
        reject_observation(
            engine,
            work,
            fingerprint,
            "Merged PR has failing CI; a corrective increment is required",
        )
        return False
    if not all(gate["ready"] for gate in gates):
        wait_for_checks(
            engine,
            work,
            fingerprint,
            "Merged PR is waiting for required GitHub checks; no coding agent is needed",
        )
        return False
    return True


def reassess_merged(engine, work):
    try:
        return _reassess_merged(engine, work)
    except OSError:
        engine._defer(
            work,
            "Acceptance policy is unavailable; post-merge reconciliation is required",
        )
        return None


def _reassess_merged(engine, work):
    gates, fingerprint = observation(engine, work)
    if not gate_checks(engine, work, gates, fingerprint):
        return None
    data = work["data"]
    checkpoint = data.get("post_merge_validation", {})
    if (
        checkpoint.get("fingerprint") != fingerprint
        or checkpoint.get("status") != "passed"
    ):
        ids = []
        for pr, gate in zip(data["pull_requests"], gates):
            actual_ci = bool(gate.get("successful"))
            ids.append(
                engine.state.evidence(
                    work["id"],
                    {
                        "kind": "github_ci" if actual_ci else "github_delivery",
                        "passed": True,
                        "candidate_set": data["acceptance_candidate"],
                        "config_digest": data["acceptance_config_digest"],
                        "repository": pr["repository"],
                        "revision": pr["observed_head"],
                        "observation_fingerprint": fingerprint,
                        "checks": pr.get("checks", []),
                        "gate": gate,
                        "scope": "PR candidate head; does not establish merge-commit or default-branch CI",
                        "ci_executed": actual_ci,
                        "summary": "Observed successful PR-head CI"
                        if actual_ci
                        else "No checks required by factory policy; CI execution is unproven",
                    },
                )
            )
        data["post_merge_validation"] = {
            "fingerprint": fingerprint,
            "status": "passed",
            "evidence_ids": ids,
        }
        work = engine.state.update_work(
            work["id"], data=data, event="validation.remote_ci.passed"
        )
    if data.get("post_merge_acceptance", {}).get("fingerprint") == fingerprint:
        return work
    config_digest = digest(resolved_bundle(engine.home, engine.config))
    try:
        decision = engine._decide(
            work,
            {"complete", "advance", "revise", "defer"},
            "complete",
            evaluation="acceptance",
            report={
                "stage": "post_merge",
                "pull_requests": data["pull_requests"],
                "delivery_evidence_ids": data["post_merge_validation"][
                    "evidence_ids"
                ],
                "boundary": "PRs are already merged. Reassess criteria using the newly observed CI. Passing delivery checks are not unfinished coding work. No-checks-required is not proof CI ran; PR-head CI does not prove default-branch CI.",
            },
        )
        if digest(resolved_bundle(engine.home, engine.config)) != config_digest:
            engine._defer(
                work,
                "Acceptance policy changed during post-merge assessment; reconciliation required",
            )
            return None
        if decision.action == "defer":
            engine._defer(work, decision.rationale)
            return None
        if decision.action == "revise":
            confirmed = confirm_merged(engine, work, fingerprint)
            if confirmed is None:
                return None
            work = confirmed
            if work["revisions"] >= engine.config.max_revisions:
                engine._defer(
                    work,
                    "Post-merge revision budget exhausted: "
                    + decision.rationale,
                )
                return None
            work = engine.state.update_work(
                work["id"], revisions=work["revisions"] + 1
            )
            engine.github.continue_after_merge(
                work, decision, [decision.rationale]
            )
            return None
        intent, remaining = acceptance_intent(
            decision,
            len(work["payload"]["acceptance"]),
            {
                item["id"]
                for item in current_evidence(
                    engine.state, engine.home, engine.config, work
                )
            },
        )
    except ValueError:
        engine._failure(
            work["id"],
            Failure(
                code="coordinator.failed",
                summary="Post-merge acceptance response was invalid; reassessment will remain at the merged candidate",
                source="coordinator",
            ),
        )
        return None
    except CoordinatorError as error:
        engine._failure(work["id"], error.failure)
        return None
    data["acceptance_decision"] = intent.model_dump()
    data["post_merge_acceptance"] = {
        "fingerprint": fingerprint,
        "decision": intent.model_dump(),
    }
    data["remaining_after_merge"] = remaining
    return engine.state.update_work(
        work["id"],
        data=data,
        control="ready",
        wake_at=0,
        event="acceptance.post_merge_recorded",
    )


def confirm_merged(engine, work, expected_fingerprint=None):
    try:
        return _confirm_merged(engine, work, expected_fingerprint)
    except OSError:
        engine._defer(
            work,
            "Acceptance policy is unavailable before closure; reconciliation required",
        )
        return None


def _confirm_merged(engine, work, expected_fingerprint):
    engine.github._current_work(work)
    data = work["data"]
    if (
        digest(resolved_bundle(engine.home, engine.config))
        != data["acceptance_config_digest"]
    ):
        engine._defer(
            work,
            "Acceptance policy changed before closure; reconciliation required",
        )
        return None
    observed = engine.github.adapter.poll_pull_requests(data["pull_requests"])
    if observed["state"] != "merged" or any(
        pr.get("head_changed") for pr in observed["pull_requests"]
    ):
        engine._defer(
            work,
            "Merged PR identity changed before closure; reconciliation required",
        )
        return None
    data["pull_requests"] = observed["pull_requests"]
    gates, fingerprint = observation(engine, work)
    if not gate_checks(engine, work, gates, fingerprint):
        return None
    if (
        expected_fingerprint
        or data.get("post_merge_acceptance", {}).get("fingerprint")
    ) != fingerprint:
        wait_for_checks(
            engine,
            work,
            fingerprint,
            "GitHub check observations changed during acceptance; reassessing before closure",
        )
        return None
    engine.github._current_work(work)
    return work
