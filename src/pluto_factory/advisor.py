"""The advisor role: turns a block into a terse, schema-checked decision.

Read-only by construction: the model gets no tools and no filesystem; it sees
only the evidence packet the coordinator builds. A reply that breaks the
schema (word caps, exactly one recommended option) is rejected and retried
with the violation named; if every attempt fails the deterministic fallback
is used, so a blocked item is never left without a decision.
"""

import asyncio
import json
import os
import re
from pathlib import Path

from pydantic import Field, field_validator, model_validator

from . import framework_sources
from .config import advisor_prompt
from .credentials import credential_environment
from .models import Contract

ADVISOR_RETRIES = 2
ADVISOR_TIMEOUT_SECONDS = 300
READ_ONLY_TOOLS = ("Read", "Grep", "Glob")
DENIED_TOOLS = (
    "Bash",
    "Write",
    "Edit",
    "NotebookEdit",
    "WebFetch",
    "WebSearch",
    "Task",
)
PROBLEM_WORDS = 40
DETAIL_WORDS = 15
LABEL_WORDS = 8
EFFECT_WORDS = 25
TRADEOFF_WORDS = 25
WHY_WORDS = 25


def words(text: str) -> int:
    return len(text.split())


def _capped(value: str, limit: int, name: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError(f"{name} is empty")
    if words(value) > limit:
        raise ValueError(f"{name} has {words(value)} words; cap is {limit}")
    return value


LOCUS = re.compile(r"^(app|framework:[A-Za-z0-9@/_.-]+)$")
FIX_WORDS = 25


class AdviceOption(Contract):
    label: str
    effect: str
    tradeoff: str
    # app | framework:<library>; framework options carry a one-line fix.
    locus: str = "app"
    fix: str = ""
    recommended: bool = False

    @field_validator("locus")
    @classmethod
    def _locus(cls, value):
        value = value.strip()
        if not LOCUS.fullmatch(value):
            raise ValueError("locus must be 'app' or 'framework:<library>'")
        return value

    @model_validator(mode="after")
    def _framework_fix(self):
        framework = self.locus.startswith("framework:")
        if framework:
            if "\n" in self.fix:
                raise ValueError("fix must be one line")
            self.fix = _capped(self.fix, FIX_WORDS, "framework fix summary")
        elif self.fix.strip():
            raise ValueError("only framework options carry a fix summary")
        return self

    @property
    def library(self):
        return self.locus.partition(":")[2] if self.locus != "app" else ""

    @field_validator("label")
    @classmethod
    def _label(cls, value):
        return _capped(value, LABEL_WORDS, "option label")

    @field_validator("effect")
    @classmethod
    def _effect(cls, value):
        return _capped(value, EFFECT_WORDS, "option effect")

    @field_validator("tradeoff")
    @classmethod
    def _tradeoff(cls, value):
        return _capped(value, TRADEOFF_WORDS, "option tradeoff")


class Advice(Contract):
    problem: str
    details: list[str] = Field(default_factory=list, max_length=6)
    options: list[AdviceOption] = Field(min_length=2, max_length=4)
    why: str

    @field_validator("problem")
    @classmethod
    def _problem(cls, value):
        return _capped(value, PROBLEM_WORDS, "problem")

    @field_validator("details")
    @classmethod
    def _details(cls, values):
        return [_capped(v, DETAIL_WORDS, "detail bullet") for v in values]

    @field_validator("why")
    @classmethod
    def _why(cls, value):
        value = _capped(value, WHY_WORDS, "why")
        if "\n" in value:
            raise ValueError("why must be one line")
        return value

    @model_validator(mode="after")
    def _one_recommended(self):
        if sum(option.recommended for option in self.options) != 1:
            raise ValueError("exactly one option must be recommended")
        if len({o.label for o in self.options}) != len(self.options):
            raise ValueError("option labels must be unique")
        return self


def terse(text: str, limit: int = PROBLEM_WORDS) -> str:
    """Deterministic shortening: first words of the first line, cap kept."""
    flat = " ".join(str(text or "").split())
    pieces = flat.split()
    return " ".join(pieces[:limit]) or "Blocked; reason unrecorded"


def fallback_advice(reason: str, *, pull_request: bool = False) -> Advice:
    """Advice without a model: raw reason, two safe, generic options."""
    if pull_request:
        options = [
            AdviceOption(
                label="I will handle it",
                effect="Owner acts on the PR. Factory keeps tracking.",
                tradeoff="No automatic progress until owner acts.",
                recommended=True,
            ),
            AdviceOption(
                label="Re-check later",
                effect="Leave as is; next poll re-evaluates the PR.",
                tradeoff="May re-escalate unchanged.",
            ),
        ]
        why = "Block needs a human step on GitHub."
    else:
        options = [
            AdviceOption(
                label="Retry as planned",
                effect="Resume work; same plan, answer added as guidance.",
                tradeoff="May block again on the same cause.",
                recommended=True,
            ),
            AdviceOption(
                label="Stop; I will handle it manually",
                effect="Resume with guidance to stand down.",
                tradeoff="No progress until owner acts.",
            ),
        ]
        why = "Cheapest first step; reversible."
    return Advice(problem=terse(reason), details=[], options=options, why=why)


def evidence_packet(state, decision, reason, extra=None, sources=None):
    """Bounded read-only context for the advisor."""
    packet = {"block_reason": reason[:4000], **(extra or {})}
    if sources:
        packet["framework_sources"] = framework_sources.digest(sources)
    work_id = decision.get("work_id")
    if work_id:
        work = state.get_work(work_id)
        reports = work["data"].get("reports", {})
        packet["work"] = {
            "title": work["title"],
            "phase": work["phase"],
            "instruction": work["payload"].get("instruction", "")[:1500],
            "acceptance": work["payload"].get("acceptance", [])[:10],
            "repositories": work["payload"].get("repositories", []),
            "revisions": work["revisions"],
            "recoveries": work["recoveries"],
            "revision_reason": (work["data"].get("revision_reason") or "")[
                :800
            ],
            "reports": {
                role: {
                    key: report.get(key)
                    for key in ("outcome", "summary", "remaining_work")
                }
                for role, report in reports.items()
            },
        }
        rows = state.db.execute(
            "SELECT data FROM evidence WHERE work_id=? "
            "ORDER BY created DESC LIMIT 4",
            (work_id,),
        ).fetchall()
        packet["recent_evidence"] = [
            {
                key: (value[-600:] if isinstance(value, str) else value)
                for key, value in json.loads(row["data"]).items()
                if key
                in {
                    "kind",
                    "passed",
                    "exit_code",
                    "command",
                    "repository",
                    "stdout",
                    "stderr",
                }
            }
            for row in rows
        ]
        packet["human_guidance"] = [
            g["text"][:500] for g in state.guidance(work_id)[-3:]
        ]
    return packet


class Advisor:
    def __init__(self, home: Path, config, runner=None):
        self.home = Path(home)
        self.config = config
        self.runner = runner

    def advise(self, packet: dict, *, pull_request=False):
        """(Advice, source): source is 'model' or 'fallback'."""
        reason = packet.get("block_reason", "")
        if self.runner is None and self.config.coordinator == "rules":
            return fallback_advice(
                reason, pull_request=pull_request
            ), "fallback"
        runner = self.runner or self._model
        prompt = advisor_prompt(self.home)
        problem = None
        for _ in range(ADVISOR_RETRIES + 1):
            payload = dict(packet)
            if problem:
                payload["contract_error"] = {
                    "message": "Previous reply violated the schema. Fix it.",
                    "problems": problem,
                }
            try:
                reply = runner(prompt, json.dumps(payload))
                if isinstance(reply, str):
                    reply = json.loads(reply)
                if not isinstance(reply, Advice):
                    reply = Advice.model_validate(reply)
                self._known_libraries(reply, packet)
                return reply, "model"
            except (ValueError, TypeError) as error:
                problem = str(error)[:1500]
            except Exception as error:  # noqa: BLE001 - provider failure
                problem = None
                self.last_error = type(error).__name__
                break
        return fallback_advice(reason, pull_request=pull_request), "fallback"

    @staticmethod
    def _known_libraries(advice, packet):
        known = {
            item["library"].lower().removeprefix("@plutoframework/")
            for item in packet.get("framework_sources", [])
        }
        for option in advice.options:
            library = option.library.lower().removeprefix("@plutoframework/")
            if library and known and library not in known:
                raise ValueError(
                    f"unknown framework library {option.library!r}; "
                    f"known: {', '.join(sorted(known))}"
                )

    # -- default model runner -------------------------------------------

    def _model_name(self):
        name = self.config.advisor_model_name()
        if self.config.advisor_provider == "anthropic":
            return name.removeprefix("anthropic:")
        return name

    def sources(self):
        return framework_sources.resolve(self.config)

    def _model(self, prompt, payload):
        sources = self.sources()
        if self.config.coordinator_runtime == "claude_agent_sdk":
            return self._claude(prompt, payload, sources)
        from pydantic_ai import Agent

        reader = framework_sources.ReferenceReader(sources)
        credentials = credential_environment(self.home)
        previous = {key: os.environ.get(key) for key in credentials}
        try:
            os.environ.update(credentials)
            agent = Agent(
                self._model_name(),
                output_type=Advice,
                instructions=prompt,
                retries=1,
                model_settings={
                    "timeout": 90,
                    "openai_reasoning_effort": self.config.advisor.reasoning,
                },
            )

            @agent.tool_plain
            def list_dir(library: str, path: str = "") -> list[str]:
                """List a directory of a Pluto library (read-only)."""
                return reader.list_dir(library, path)

            @agent.tool_plain
            def read_file(library: str, path: str) -> str:
                """Read a source file of a Pluto library (read-only)."""
                return reader.read_file(library, path)

            @agent.tool_plain
            def search(library: str, pattern: str) -> list[str]:
                """Regex-search a Pluto library's sources (read-only)."""
                return reader.search(library, pattern)

            return agent.run_sync(payload).output
        finally:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def claude_options(self, prompt, sources):
        """SDK options: read-only tools confined to the framework sources."""
        from claude_agent_sdk import ClaudeAgentOptions

        return ClaudeAgentOptions(
            model=self._model_name(),
            system_prompt=prompt,
            tools=list(READ_ONLY_TOOLS),
            allowed_tools=list(READ_ONLY_TOOLS),
            disallowed_tools=list(DENIED_TOOLS),
            permission_mode="dontAsk",
            setting_sources=[],
            add_dirs=[item["path"] for item in sources],
            max_turns=12,
            cwd=str(self.home),
            effort=self.config.advisor.reasoning,
            output_format={
                "type": "json_schema",
                "schema": Advice.model_json_schema(),
            },
        )

    def _claude(self, prompt, payload, sources):
        from claude_agent_sdk import ResultMessage, query

        options = self.claude_options(prompt, sources)

        async def run():
            result = None
            async for message in query(prompt=payload, options=options):
                if isinstance(message, ResultMessage):
                    result = message
            return result

        result = asyncio.run(asyncio.wait_for(run(), ADVISOR_TIMEOUT_SECONDS))
        if (
            result is None
            or result.is_error
            or result.structured_output is None
        ):
            raise RuntimeError("advisor produced no structured output")
        return result.structured_output
