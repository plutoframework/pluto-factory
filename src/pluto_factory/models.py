"""Contracts shared by the coordinator, adapters and reporting extension."""

import os
import re
from pathlib import Path
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)


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


class TrackedRepository(Contract):
    """An extra GitHub repository whose open PRs the factory watches.

    ``local_path`` is the checkout workers repair from; ``checks``/``qa``
    mirror ``[[repositories]]``.
    """

    repo: str = Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
    local_path: str = ""
    checks: list[list[str]] = Field(default_factory=list)
    qa: list[list[str]] = Field(default_factory=list)

    @property
    def name(self) -> str:
        slug = re.sub(r"[^a-z0-9_-]+", "-", self.repo.lower())
        return ("t-" + slug)[:32]


ANTHROPIC_ADVISOR_MODEL = "claude-opus-5-5"
OPENAI_ADVISOR_MODEL = "gpt-6.1-sol"


class AdvisorPolicy(Contract):
    """Read-only advisor: reasoning effort and extra framework sources.

    ``reference_paths`` are local checkouts of Pluto libraries (CLI,
    pluto-core/rbac/auth/jobs, the TypeScript SDK) added to the sources
    resolved from the Go module cache and node_modules at pinned versions.
    """

    reasoning: str = "high"
    reference_paths: list[str] = Field(default_factory=list)

    @field_validator("reference_paths")
    @classmethod
    def _paths(cls, paths):
        for raw in paths:
            if not raw or "\0" in raw or ".." in Path(raw).parts:
                raise ValueError(f"invalid reference path: {raw!r}")
            if not os.path.isabs(os.path.expanduser(raw)):
                raise ValueError(f"reference path must be absolute: {raw!r}")
        return paths


class CliPin(Contract):
    """Where the CLI pins a library: a file and a regex with one group.

    The group yields a commit-ish: a SHA, a tag, or a Go pseudo-version
    (whose trailing 12 hex digits name the commit).
    """

    path: str = Field(min_length=1)
    pattern: str = Field(min_length=1)


class FrameworkPolicy(Contract):
    """Where framework gaps go: library name -> upstream GitHub repository."""

    upstream: dict[str, str] = Field(default_factory=dict)
    # Informational only: a foreign factory never marks upstream work ready.
    proposed_label: str = Field(default="factory:proposed", min_length=1)
    file_upstream: Literal["on_decision", "auto"] = "on_decision"
    # Extra private terms the upstream scrubber must never let through.
    redact_terms: list[str] = Field(default_factory=list)
    # Dependent side: a fix is consumable once the library revision the
    # newest CLI revision pins contains it. Empty cli_repo disables the check.
    cli_repo: str = ""
    cli_ref: str = "main"
    cli_pins: dict[str, CliPin] = Field(default_factory=dict)

    @field_validator("upstream")
    @classmethod
    def _repos(cls, mapping):
        for library, repo in mapping.items():
            if not library.strip() or not re.fullmatch(
                r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo
            ):
                raise ValueError(
                    f"upstream {library!r} needs an owner/name repository"
                )
        return mapping

    def repository(self, library: str) -> str:
        def norm(name):
            name = name.strip().lower().removeprefix("@plutoframework/")
            return name.removeprefix("pluto-")

        for name, repo in self.upstream.items():
            if norm(name) == norm(library):
                return repo
        return ""


class PropagationRule(Contract):
    library: str = Field(min_length=1)
    # Sibling modules in the same repo that must land the fix pinned to the
    # merge commit before the CLI can consume it (e.g. a Go store module).
    sibling_modules: list[str] = Field(default_factory=list)
    cli_bump: bool = False


class PropagationPolicy(Contract):
    """Receiving side: follow-up work that makes a library fix consumable."""

    cli_repo: str = ""
    rules: list[PropagationRule] = Field(default_factory=list)

    def rule(self, library: str):
        return next(
            (r for r in self.rules if r.library.lower() == library.lower()),
            None,
        )


class AutonomyPolicy(Contract):
    """When the advisor's recommendation may be applied without the owner.

    ``auto_obvious`` lets a high-confidence, reversible recommendation in an
    allowlisted category apply itself after ``grace_seconds`` (the owner can
    override); ``ask_all`` always asks. ``max_config`` bounds the numeric
    config keys an automatic config change may touch.
    """

    mode: Literal["auto_obvious", "ask_all"] = "auto_obvious"
    grace_seconds: int = Field(default=600, ge=0, le=86400)
    max_config: dict[str, float] = Field(default_factory=dict)


class TriagePolicy(Contract):
    """Optional receiving-side triage of new issues (off by default).

    Model, provider and reasoning default to the strongest tier like the
    advisor. ``trusted_authors`` may be auto-accepted, as may repository
    collaborators with write access; everyone else is capped at NEEDS_HUMAN.
    """

    enabled: bool = False
    provider: str = ""
    model: str = ""
    reasoning: str = "high"
    watch_label: str = ""
    reject_label: str = Field(default="factory:rejected", min_length=1)
    trusted_authors: list[str] = Field(default_factory=list)


class ModelProfile(Contract):
    provider: str = "openai"
    model: str = "gpt-6.1-sol"
    reasoning: str = "high"


CLAUDE_PROFILES = {
    "worker": {
        "provider": "anthropic",
        "model": "claude-opus-5-5",
        "reasoning": "high",
    },
    "reviewer": {
        "provider": "anthropic",
        "model": "claude-opus-5-5",
        "reasoning": "high",
    },
    "qa_model": {
        "provider": "anthropic",
        "model": "claude-sonnet-5-5",
        "reasoning": "medium",
    },
}


CLAUDE_COORDINATOR_MODEL = "claude-sonnet-5-5"


class ImprovementPolicy(Contract):
    mode: Literal["disabled", "propose", "automatic"] = "propose"
    allowed_areas: list[str] = Field(
        default_factory=lambda: ["implementer_prompt", "qa_guidance"]
    )
    evaluations: list[list[str]] = Field(default_factory=list)


class DebugPolicy(Contract):
    """Opt-in trace of every worker tool call, edited file and decision."""

    enabled: bool = False
    level: Literal["trace", "full"] = "trace"
    max_output_bytes: int = Field(default=4096, ge=256, le=1_048_576)
    redact: bool = True


DOCKER_SOCKET = "/var/run/docker.sock"
ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
DOMAIN = re.compile(
    r"^(?:\*\.)?[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)*$"
)
# Variables that would hand a worker a host control channel.
FORBIDDEN_ENV = {
    "HC_TERMINAL_SOCKET",
    "DOCKER_HOST",
    "SSH_AUTH_SOCK",
    # Workers never publish: no GitHub credentials may reach them.
    "GH_TOKEN",
    "GITHUB_TOKEN",
    "GH_ENTERPRISE_TOKEN",
    "GITHUB_ENTERPRISE_TOKEN",
}


class SandboxPolicy(Contract):
    """nono sandbox for workers when ``isolation = "required"``.

    ``profile`` is a nono profile name or path; empty uses the bundled
    sealed profile. Any profile must block host sockets (Herdr, docker,
    dbus) or dispatch is refused. ``read_paths``/``write_paths`` grant extra
    absolute host paths (toolchains, caches); ``allow_domains`` restricts
    network egress; ``env_passthrough`` names extra host environment
    variables copied to the worker (never put secrets here: values travel
    on the terminal command line). ``allow_docker`` is an explicit opt-in
    that exposes the Docker socket, which is equivalent to root on the
    host: a container escape is a host escape.
    """

    profile: str = ""
    read_paths: list[str] = Field(default_factory=list)
    write_paths: list[str] = Field(default_factory=list)
    allow_domains: list[str] = Field(default_factory=list)
    env_passthrough: list[str] = Field(default_factory=list)
    allow_docker: bool = False

    @model_validator(mode="before")
    @classmethod
    def _legacy_names(cls, value):
        if isinstance(value, dict):
            value = dict(value)
            for old, new in (("read", "read_paths"), ("write", "write_paths")):
                if old in value:
                    if new in value:
                        raise ValueError(f"use only {new}, not {old}")
                    value[new] = value.pop(old)
        return value

    @field_validator("read_paths", "write_paths")
    @classmethod
    def _paths(cls, paths):
        for raw in paths:
            if not raw or "\0" in raw:
                raise ValueError("sandbox paths must be non-empty")
            expanded = os.path.expanduser(raw)
            if not os.path.isabs(expanded):
                raise ValueError(f"sandbox path must be absolute: {raw!r}")
            if ".." in Path(expanded).parts:
                raise ValueError(f"sandbox path must not contain '..': {raw!r}")
        return paths

    @field_validator("allow_domains")
    @classmethod
    def _domains(cls, domains):
        for domain in domains:
            if not DOMAIN.fullmatch(domain):
                raise ValueError(f"invalid sandbox domain: {domain!r}")
        return domains

    @field_validator("env_passthrough")
    @classmethod
    def _environment(cls, names):
        for name in names:
            if not ENV_NAME.fullmatch(name):
                raise ValueError(f"invalid environment variable: {name!r}")
            if name in FORBIDDEN_ENV or name.startswith(("HC_", "HERDR")):
                raise ValueError(f"{name} would expose a host control channel")
        return names


# Roles that edit the worktree and may call the factory_check tool.
WRITER_ROLES = frozenset({"implementer", "merge_manager"})

PHASES = (
    "queued",
    "implementing",
    "checking",
    "reviewing",
    "preparing",
    "qa_checks",
    "qa",
    "pushing",
    "integrating",
)


class ConcurrencyPolicy(Contract):
    """Parallel workers. The defaults reproduce the serial factory exactly.

    ``max_per_repository`` defaults to ``max_workers``; ``repositories``
    overrides it per repository name. ``max_per_phase`` caps items running
    in a phase (e.g. ``checking``), counting long check commands.
    ``avoid_overlap`` stops items that may touch the same files from running
    together: ``repository`` (default when parallel) serializes items per
    repository; ``paths`` estimates touched paths and falls back to
    repository-level when it cannot. ``min_free_memory_mb`` and
    ``max_load_average`` hold new starts while the host is under pressure.
    """

    max_workers: int = Field(default=1, ge=1, le=8)
    max_per_repository: int | None = Field(default=None, ge=1, le=8)
    repositories: dict[str, int] = Field(default_factory=dict)
    max_per_phase: dict[str, int] = Field(default_factory=dict)
    avoid_overlap: Literal["none", "repository", "paths"] | None = None
    start_stagger_seconds: float = Field(default=30, ge=0)
    min_free_memory_mb: int | None = Field(default=None, ge=0)
    max_load_average: float | None = Field(default=None, gt=0)
    # Serialize coordinator model calls unless explicitly allowed.
    coordinator_parallel_decisions: bool = False

    @field_validator("repositories")
    @classmethod
    def _repository_limits(cls, value):
        if any(not 1 <= limit <= 8 for limit in value.values()):
            raise ValueError("repository limits must be 1..8")
        return value

    @field_validator("max_per_phase")
    @classmethod
    def _phase_limits(cls, value):
        for phase, limit in value.items():
            if phase not in PHASES:
                raise ValueError(f"unknown phase {phase!r}")
            if not 1 <= limit <= 8:
                raise ValueError("phase limits must be 1..8")
        return value

    @property
    def parallel(self) -> bool:
        return self.max_workers > 1

    @property
    def overlap(self) -> str:
        if self.avoid_overlap is not None:
            return self.avoid_overlap
        return "repository" if self.parallel else "none"

    def repository_limit(self, name: str) -> int:
        if name in self.repositories:
            return self.repositories[name]
        return self.max_per_repository or self.max_workers


class MergeManagerPolicy(Contract):
    """Role that resolves merge conflicts with the moving default branch.

    Default model: empty provider/model/reasoning inherit the reviewer's
    profile (set the reviewer to a Fable-tier model, or override here). ``enabled``
    unset follows parallelism: on when ``concurrency.max_workers > 1``, so
    serial factories keep routing conflicts to the implementer.
    """

    provider: str = ""
    model: str = ""
    reasoning: str = ""
    enabled: bool | None = None
    max_merge_attempts: int = Field(default=2, ge=1, le=10)


class FactoryConfig(Contract):
    schema_version: Literal[1] = 1
    name: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,31}$")
    repositories: list[Repository] = Field(min_length=1)
    tracked_repositories: list[TrackedRepository] = Field(default_factory=list)
    harness: Literal["pi", "claude", "fixture"] = "pi"
    coordinator: Literal["model", "rules"] = "model"
    coordinator_runtime: Literal["pydantic_ai", "claude_agent_sdk"] = (
        "pydantic_ai"
    )
    coordinator_model: str = "openai-codex:gpt-6.1-terra"
    auth_mode: Literal["existing", "codex", "api_key"] = "existing"
    api_key_env: str = "OPENAI_API_KEY"
    worker: ModelProfile = Field(default_factory=ModelProfile)
    reviewer: ModelProfile = Field(default_factory=ModelProfile)
    # Strongest tier by default; set to e.g. "claude-fable-5-2" to override.
    advisor_model: str = ""
    advisor: AdvisorPolicy = Field(default_factory=lambda: AdvisorPolicy())
    framework: FrameworkPolicy = Field(default_factory=FrameworkPolicy)
    triage: TriagePolicy = Field(default_factory=TriagePolicy)
    autonomy: AutonomyPolicy = Field(default_factory=AutonomyPolicy)
    propagation: PropagationPolicy = Field(default_factory=PropagationPolicy)
    concurrency: ConcurrencyPolicy = Field(default_factory=ConcurrencyPolicy)
    merge_manager: MergeManagerPolicy = Field(
        default_factory=MergeManagerPolicy
    )
    qa_model: ModelProfile = Field(
        default_factory=lambda: ModelProfile(
            model="gpt-6.1-terra", reasoning="medium"
        )
    )
    isolation: Literal["cooperative", "required"] = "required"
    trust_registered_repositories: bool = False
    # Claude workers bypass all permission checks (unattended factories).
    dangerously_skip_permissions: bool = False
    # Keep worker terminals after their run ends (inspection/Pi rebinding).
    keep_finished_sessions: bool = False
    integration: Literal["managed", "github"] = "managed"
    github_issue_label: str = Field(default="factory:ready", min_length=1)
    github_poll_seconds: int = Field(default=30, ge=1)
    github_auto_merge: bool = False
    github_require_up_to_date: bool = False
    github_max_resyncs: int = Field(default=3, ge=0, le=20)
    github_required_checks: list[str] = Field(default_factory=list)
    close_source_issues: bool = True
    herdr_space: str = ""
    slack_webhook_env: str = ""
    max_recoveries: int = Field(default=3, ge=0, le=20)
    max_revisions: int = Field(default=5, ge=0, le=100)
    phase_timeout_seconds: int = Field(default=900, ge=1)
    # Implementers legitimately run longer; unset falls back to the above.
    implementer_phase_timeout_seconds: int | None = Field(default=None, ge=1)
    command_timeout_seconds: int = Field(default=120, ge=1)
    # Pull-request tracking: poll cadence and bounded self-healing.
    pr_poll_seconds: int = Field(default=300, ge=1)
    pr_max_repairs: int = Field(default=3, ge=0, le=20)
    pr_max_repairs_total: int = Field(default=6, ge=0, le=100)
    retry_base_seconds: float = Field(default=5, ge=0)
    self_improvement: ImprovementPolicy = Field(
        default_factory=ImprovementPolicy
    )
    debug: DebugPolicy = Field(default_factory=DebugPolicy)
    sandbox: SandboxPolicy = Field(default_factory=SandboxPolicy)

    @model_validator(mode="after")
    def _labels_never_ready(self):
        labels = {
            self.github_issue_label,
            self.framework.proposed_label,
            self.triage.reject_label,
        }
        if len(labels) != 3:
            raise ValueError(
                "ready, proposed and rejected issue labels must all differ"
            )
        return self

    @model_validator(mode="after")
    def _advisor_model(self):
        if self.advisor_model:
            if not re.fullmatch(r"[A-Za-z0-9._:/-]+", self.advisor_model):
                raise ValueError("advisor_model has invalid characters")
            if self.advisor_provider == "anthropic" and not (
                self.advisor_model.startswith("claude-")
            ):
                raise ValueError(
                    "advisor_model must be a claude-* model on Anthropic"
                )
        return self

    @property
    def merge_manager_enabled(self) -> bool:
        enabled = self.merge_manager.enabled
        return self.concurrency.parallel if enabled is None else enabled

    def role_profile(self, role: str) -> ModelProfile:
        """Model profile for a worker role; the merge manager inherits."""
        if role == "merge_manager":
            policy = self.merge_manager
            base = self.reviewer
            return ModelProfile(
                provider=policy.provider or base.provider,
                model=policy.model or base.model,
                reasoning=policy.reasoning or base.reasoning,
            )
        return {
            "implementer": self.worker,
            "reviewer": self.reviewer,
        }.get(role, self.qa_model)

    @property
    def advisor_provider(self) -> str:
        anthropic = (
            self.coordinator_runtime == "claude_agent_sdk"
            or self.harness == "claude"
        )
        return "anthropic" if anthropic else "openai"

    def triage_model_name(self) -> str:
        """Triage model: override, else the provider's strongest default."""
        if self.triage.model:
            return self.triage.model
        saved, self.advisor_model = self.advisor_model, ""
        try:
            return self.advisor_model_name()
        finally:
            self.advisor_model = saved

    def advisor_model_name(self) -> str:
        """Configured override, else the provider's strongest default."""
        if self.advisor_model:
            return self.advisor_model
        if self.advisor_provider == "anthropic":
            return ANTHROPIC_ADVISOR_MODEL
        prefix, sep, _ = self.coordinator_model.partition(":")
        return (
            f"{prefix}:{OPENAI_ADVISOR_MODEL}" if sep else OPENAI_ADVISOR_MODEL
        )


def workable_repositories(config) -> list[Repository]:
    """Registered repositories plus tracked ones with a local checkout."""
    extra = [
        Repository(
            name=item.name,
            path=item.local_path,
            access="write",
            github=item.repo,
            checks=item.checks,
            qa=item.qa,
        )
        for item in getattr(config, "tracked_repositories", [])
        if item.local_path
    ]
    known = {repo.name for repo in config.repositories}
    return [*config.repositories, *(r for r in extra if r.name not in known)]


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
    result: Literal["passed", "incomplete", "failed", "unverifiable", "waived"]
    evidence_refs: list[str] = Field(default_factory=list)
    explanation: str = Field(min_length=1)


class Decision(Contract):
    action: Literal["advance", "revise", "retry", "defer", "complete"]
    rationale: str = Field(
        min_length=1,
        description="Telegraphic, minimal words, bullets over prose.",
    )
    acceptance: list[CriterionAssessment] = Field(default_factory=list)


class Failure(Contract):
    code: str
    summary: str
    source: Literal["worker", "adapter", "runtime", "coordinator"]
    uncertain: bool = False
    retry_after_seconds: float | None = Field(default=None, ge=0)
