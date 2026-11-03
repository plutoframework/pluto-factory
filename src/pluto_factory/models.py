"""Contracts shared by the coordinator, adapters and reporting extension."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Repository(Contract):
    name: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,31}$")
    path: str
    access: Literal["read", "write"] = "write"
    github: str = Field(
        default="", pattern=r"^(?:[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)?$"
    )
    checks: list[list[str]] = Field(default_factory=list)
    qa: list[list[str]] = Field(default_factory=list)
    flag_environments: list[dict[str, str]] = Field(
        default_factory=lambda: [{}]
    )


class ModelProfile(Contract):
    provider: str = "openai"
    model: str = "gpt-6.1-sol"
    reasoning: str = "high"


class ImprovementPolicy(Contract):
    mode: Literal["disabled", "propose", "automatic"] = "propose"
    allowed_areas: list[str] = Field(
        default_factory=lambda: ["implementer_prompt", "qa_guidance"]
    )
    evaluations: list[list[str]] = Field(default_factory=list)


class FactoryConfig(Contract):
    schema_version: Literal[1] = 1
    name: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,31}$")
    repositories: list[Repository] = Field(min_length=1)
    harness: Literal["pi", "fixture"] = "pi"
    coordinator: Literal["model", "rules"] = "model"
    coordinator_model: str = "openai-codex:gpt-6.1-terra"
    auth_mode: Literal["existing", "codex", "api_key"] = "existing"
    api_key_env: str = "OPENAI_API_KEY"
    worker: ModelProfile = Field(default_factory=ModelProfile)
    reviewer: ModelProfile = Field(default_factory=ModelProfile)
    qa_model: ModelProfile = Field(
        default_factory=lambda: ModelProfile(
            model="gpt-6.1-terra", reasoning="medium"
        )
    )
    isolation: Literal["cooperative", "required"] = "required"
    trust_registered_repositories: bool = False
    integration: Literal["managed", "github"] = "managed"
    github_issue_label: str = Field(default="factory:ready", min_length=1)
    github_poll_seconds: int = Field(default=30, ge=1)
    github_auto_merge: bool = False
    github_required_checks: list[str] = Field(default_factory=list)
    close_source_issues: bool = True
    herdr_space: str = ""
    slack_webhook_env: str = ""
    max_recoveries: int = Field(default=3, ge=0, le=20)
    max_revisions: int = Field(default=5, ge=0, le=100)
    phase_timeout_seconds: int = Field(default=900, ge=1)
    command_timeout_seconds: int = Field(default=120, ge=1)
    retry_base_seconds: float = Field(default=5, ge=0)
    self_improvement: ImprovementPolicy = Field(
        default_factory=ImprovementPolicy
    )


class Finding(Contract):
    severity: Literal["info", "warning", "required"]
    summary: str
    repository: str = ""
    location: str = ""


class Improvement(Contract):
    area: str
    reason: str
    proposed_content: str


class WorkerReport(Contract):
    outcome: Literal["ready", "revise", "defer", "error"]
    summary: str = Field(min_length=1)
    artifacts: list[str] = Field(default_factory=list)
    remaining_work: list[str] = Field(default_factory=list)
    findings: list[Finding] = Field(default_factory=list)
    reviewed_repositories: list[str] = Field(default_factory=list)
    error_code: str | None = None
    retry_after_seconds: float | None = Field(default=None, ge=0)
    suggested_next_action: str = ""
    improvements: list[Improvement] = Field(default_factory=list)


class Submission(Contract):
    title: str = Field(min_length=1)
    instruction: str = Field(min_length=1)
    acceptance: list[str] = Field(min_length=1)
    repositories: list[str] = Field(min_length=1)
    source: str = "cli"
    delivery_key: str | None = None
    depends_on: list[str] = Field(default_factory=list)
    depends_on_sources: list[str] = Field(default_factory=list)
    fixture_scenario: str | None = None


class CriterionAssessment(Contract):
    criterion: int = Field(ge=0)
    result: Literal["passed", "incomplete", "failed"]
    evidence_refs: list[str] = Field(default_factory=list)
    explanation: str = Field(min_length=1)


class Decision(Contract):
    action: Literal["advance", "revise", "retry", "defer", "complete"]
    rationale: str = Field(min_length=1)
    acceptance: list[CriterionAssessment] = Field(default_factory=list)


class Failure(Contract):
    code: str
    summary: str
    source: Literal["worker", "adapter", "runtime", "coordinator"]
    uncertain: bool = False
    retry_after_seconds: float | None = Field(default=None, ge=0)
