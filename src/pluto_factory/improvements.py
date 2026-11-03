"""Stage configuration improvements without granting policy-edit authority."""

import os
import subprocess
import time

from .config import digest, resolved_bundle
from .state import identity

AREAS = {
    "implementer_prompt": "prompts/implementer.md",
    "reviewer_prompt": "prompts/reviewer.md",
    "orchestrator_prompt": "prompts/orchestrator.md",
    "qa_guidance": "qa/guidance.md",
}


def propose(state, config, work_id, improvement):
    policy = config.self_improvement
    if policy.mode == "disabled":
        return None
    allowed = improvement.area in policy.allowed_areas
    status = "proposed" if allowed and improvement.area in AREAS else "denied"
    proposal_id = identity("improvement")
    checksum = digest(resolved_bundle(state.home, config))
    with state.db:
        state.db.execute(
            "INSERT INTO improvements "
            "(id,work_id,area,reason,content,status,base_digest,created) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (
                proposal_id,
                work_id,
                improvement.area,
                improvement.reason,
                improvement.proposed_content,
                status,
                checksum,
                time.time(),
            ),
        )
        state._event(
            "improvement.proposed",
            work_id,
            {
                "id": proposal_id,
                "area": improvement.area,
                "status": status,
            },
        )
    if status == "proposed" and policy.mode == "automatic":
        activate(state, config, proposal_id, automatic=True)
    return proposal_id


def activate(state, config, proposal_id, *, automatic=False):
    row = state.db.execute(
        "SELECT * FROM improvements WHERE id=?", (proposal_id,)
    ).fetchone()
    if row is None or row["status"] != "proposed":
        raise ValueError("improvement is not available for activation")
    if row["area"] not in AREAS or row["area"] not in (
        config.self_improvement.allowed_areas
    ):
        raise ValueError("improvement area is outside owner policy")
    if digest(resolved_bundle(state.home, config)) != row["base_digest"]:
        raise ValueError("configuration changed; reevaluate the proposal")
    if automatic and not config.self_improvement.evaluations:
        state.event("improvement.needs_evaluation", proposal_id, {})
        return {"status": "proposed", "reason": "evaluation not configured"}
    target = state.home / AREAS[row["area"]]
    candidate = state.home / "revisions" / proposal_id
    candidate.mkdir(parents=True, exist_ok=True)
    (candidate / "previous.md").write_text(target.read_text())
    (candidate / "candidate.md").write_text(row["content"])
    for command in config.self_improvement.evaluations:
        environment = os.environ.copy()
        environment["FACTORY_CANDIDATE_PROMPT"] = str(
            candidate / "candidate.md"
        )
        result = subprocess.run(
            command,
            cwd=state.home,
            env=environment,
            capture_output=True,
            text=True,
            timeout=config.command_timeout_seconds,
            check=False,
        )
        if result.returncode:
            state.event("improvement.evaluation_failed", proposal_id, {})
            return {"status": "proposed", "reason": "evaluation failed"}
    temporary = target.with_suffix(".new")
    temporary.write_text(row["content"])
    temporary.replace(target)
    with state.db:
        state.db.execute(
            "UPDATE improvements SET status='activated' WHERE id=?",
            (proposal_id,),
        )
        state._event(
            "improvement.activated",
            proposal_id,
            {
                "activation": "next_assignment",
                "previous": str(candidate),
            },
        )
    return {"status": "activated", "id": proposal_id}


def rollback(state, proposal_id):
    row = state.db.execute(
        "SELECT * FROM improvements WHERE id=?", (proposal_id,)
    ).fetchone()
    if row is None or row["status"] != "activated" or row["area"] not in AREAS:
        raise ValueError("improvement cannot be rolled back")
    target = state.home / AREAS[row["area"]]
    if target.read_text() != row["content"]:
        raise ValueError("active content changed; refusing to overwrite it")
    previous = state.home / "revisions" / proposal_id / "previous.md"
    target.write_text(previous.read_text())
    with state.db:
        state.db.execute(
            "UPDATE improvements SET status='rolled_back' WHERE id=?",
            (proposal_id,),
        )
        state._event("improvement.rolled_back", proposal_id, {})
    return {"status": "rolled_back", "id": proposal_id}
