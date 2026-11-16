"""The acceptance digest covers only what changes validation outcomes."""

import pytest
from test_attention import build

from pluto_factory.config import digest, resolved_bundle
from pluto_factory.models import (
    AdvisorPolicy,
    AutonomyPolicy,
    ConcurrencyPolicy,
    FrameworkPolicy,
    MergeManagerPolicy,
    PropagationPolicy,
    Repository,
    TrackedRepository,
    TriagePolicy,
)

EXCLUDED = {
    "advisor": AdvisorPolicy(reasoning="low"),
    "advisor_model": "claude-fable-5-2",
    "autonomy": AutonomyPolicy(mode="ask_all", grace_seconds=5),
    "framework": FrameworkPolicy(
        upstream={"pluto-cli": "plutoframework/cli"},
        redact_terms=["secret"],
    ),
    "triage": TriagePolicy(enabled=True),
    "propagation": PropagationPolicy(cli_repo="cli"),
    "tracked_repositories": [
        TrackedRepository(
            repo="plutoframework/cli", local_path="/tmp", checks=[["true"]]
        )
    ],
    "concurrency": ConcurrencyPolicy(max_workers=3),
    "merge_manager": MergeManagerPolicy(max_merge_attempts=4),
    "github_poll_seconds": 99,
    "pr_poll_seconds": 77,
    "herdr_space": "other",
    "slack_webhook_env": "HOOK",
    "max_recoveries": 9,
    "max_revisions": 9,
    "phase_timeout_seconds": 5000,
    "keep_finished_sessions": True,
    "close_source_issues": False,
}


def bundle_digest(engine, config):
    return digest(resolved_bundle(engine.home, config))


@pytest.mark.parametrize("field", sorted(EXCLUDED))
def test_operational_settings_never_drift_acceptance(tmp_path, field):
    engine, _ = build(tmp_path)
    before = bundle_digest(engine, engine.config)
    changed = engine.config.model_copy(update={field: EXCLUDED[field]})
    assert bundle_digest(engine, changed) == before


def test_validation_inputs_do_drift_acceptance(tmp_path):
    engine, _ = build(tmp_path)
    before = bundle_digest(engine, engine.config)
    config = engine.config
    repo = config.repositories[0]
    for checks in (
        {"checks": [["false"]]},
        {"qa": [["echo", "changed"]]},
    ):
        changed = config.model_copy(
            update={
                "repositories": [
                    Repository.model_validate({**repo.model_dump(), **checks})
                ]
            }
        )
        assert bundle_digest(engine, changed) != before
    worker = config.worker.model_copy(update={"model": "another-model"})
    assert (
        bundle_digest(engine, config.model_copy(update={"worker": worker}))
        != before
    )
    (engine.home / "prompts" / "orchestrator.md").write_text("new policy")
    assert bundle_digest(engine, config) != before
