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


class FrameworkPolicy(Contract):
    """Where framework gaps go: library name -> upstream GitHub repository."""

    upstream: dict[str, str] = Field(default_factory=dict)
    # Informational only: a foreign factory never marks upstream work ready.
    proposed_label: str = Field(default="factory:proposed", min_length=1)
    file_upstream: Literal["on_decision", "auto"] = "on_decision"

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
            return name.lower().removeprefix("@plutoframework/")

        for name, repo in self.upstream.items():
            if norm(name) == norm(library):
                return repo
        return ""


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
