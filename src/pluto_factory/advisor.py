"""The advisor role: turns a block into a terse, schema-checked decision.

Read-only by construction: the model gets no tools and no filesystem; it sees
only the evidence packet the coordinator builds. A reply that breaks the
schema (word caps, exactly one recommended option) is rejected and retried
with the violation named; if every attempt fails the deterministic fallback
is used, so a blocked item is never left without a decision.
"""

import contextlib
import json
import re
import time
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator, model_validator

from . import framework_sources, structured
from .config import advisor_prompt
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


UPSTREAM_TITLE_CHARS = 80
UPSTREAM_FIELD_CHARS = 2000


class UpstreamPayload(Contract):
    """Public-audience issue text: library terms only, no private context."""

    title: str
    problem: str
    repro: str
    fix: str
    acceptance: list[str] = Field(min_length=1, max_length=8)

    @field_validator("title")
    @classmethod
    def _title(cls, value):
        value = " ".join(value.split())
        if not value or len(value) > UPSTREAM_TITLE_CHARS:
            raise ValueError(
                f"upstream title must be 1-{UPSTREAM_TITLE_CHARS} characters"
            )
        return value

    @field_validator("problem", "repro", "fix")
    @classmethod
    def _text(cls, value):
        value = value.strip()
        if not value or len(value) > UPSTREAM_FIELD_CHARS:
            raise ValueError(
                f"upstream text must be 1-{UPSTREAM_FIELD_CHARS} characters"
            )
        return value

    @field_validator("acceptance")
    @classmethod
    def _acceptance(cls, values):
        values = [v.strip() for v in values]
        if not all(values) or any(len(v) > 300 for v in values):
            raise ValueError("acceptance items are 1-300 characters")
        return values


class AdviceOption(Contract):
    label: str
    effect: str
    tradeoff: str
    # app | framework:<library>; framework options carry a one-line fix.
    locus: str = "app"
    fix: str = ""
    # Required for framework options; written for a public issue tracker.
    upstream: UpstreamPayload | None = None
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
            if self.upstream is None:
                raise ValueError(
                    "framework options require an upstream payload "
                    "(title, problem, repro, fix, acceptance)"
                )
        elif self.fix.strip() or self.upstream is not None:
            raise ValueError(
                "only framework options carry a fix summary or upstream"
            )
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


AUTO_CATEGORIES = (
    "upstream_issue",
    "resume_after_fix",
    "app_fix",
    "config_change",
)
OWNER_CATEGORIES = (
    "security",
    "credentials_spend",
    "acceptance_scope",
    "public_release",
    "product_ux",
    "other",
)
Category = Literal[AUTO_CATEGORIES + OWNER_CATEGORIES]  # type: ignore[valid-type]


class ConfigChange(Contract):
    key: str
    value: float


class Advice(Contract):
    # Autonomy: the model proposes, deterministic policy decides. Owner-only
    # categories can never auto-apply whatever the model claims.
    category: Category = "other"
    autonomy: Literal["auto", "owner"] = "owner"
    autonomy_reason: str = ""
    confidence: Literal["high", "medium", "low"] = "low"
    reversible: bool = False
    config_change: ConfigChange | None = None
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

    @field_validator("autonomy_reason")
    @classmethod
    def _autonomy_reason(cls, value):
        value = " ".join(value.split())
        if words(value) > WHY_WORDS:
            raise ValueError(f"autonomy_reason cap is {WHY_WORDS} words")
        return value

    @model_validator(mode="after")
    def _autonomy_consistent(self):
        if self.autonomy == "auto" and not self.autonomy_reason:
            raise ValueError("autonomy 'auto' needs an autonomy_reason")
        if (self.category == "config_change") != (
            self.config_change is not None
        ):
            raise ValueError(
                "config_change is required for (and only for) the "
                "config_change category"
            )
        return self

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


TRANSIENT = {"timeout", "rate_limit", "crash"}
TRANSIENT_RETRIES = 2
BACKOFF_SECONDS = (5, 20)
STDERR_TAIL = 500
MESSAGE_LIMIT = 400


def _tail(value, limit):
    value = str(value or "")
    return value if len(value) <= limit else "…" + value[-limit:]


def classify(error):
    """schema | timeout | rate_limit | crash | unavailable | error."""
    name = type(error).__name__
    message = str(error).lower()
    if isinstance(error, (TimeoutError,)) or "timeout" in name.lower():
        return "timeout"
    if (
        "ratelimit" in name.lower()
        or "rate limit" in message
        or "429" in message
    ):
        return "rate_limit"
    if (
        getattr(error, "exit_code", None) is not None
        or getattr(error, "returncode", None) is not None
        or "ProcessError" in name
    ):
        return "crash"
    if name in {"UnexpectedModelBehavior", "ValidationError"} or isinstance(
        error, (ValueError, TypeError)
    ):
        return "schema"
    if "unavailable" in message:
        return "unavailable"
    return "error"


def failure_record(error, attempts):
    """Bounded, redacted account of why a model triage failed."""
    from .dashboard_data import text

    exit_code = getattr(error, "exit_code", None)
    if exit_code is None:
        exit_code = getattr(error, "returncode", None)
    cause = error.__cause__ or error.__context__
    record = {
        "kind": classify(error),
        "exception": type(error).__name__,
        "message": text(_tail(str(error), MESSAGE_LIMIT), MESSAGE_LIMIT + 20),
        "attempts": attempts,
    }
    if cause is not None:
        record["cause"] = text(
            f"{type(cause).__name__}: {_tail(str(cause), 300)}", 340
        )
    if hasattr(error, "errors") and callable(error.errors):
        with contextlib.suppress(Exception):
            record["validation_errors"] = text(
                json.dumps(error.errors(include_url=False), default=str)[:1200],
                1200,
            )
    if exit_code is not None:
        record["exit_code"] = exit_code
    stderr = getattr(error, "stderr", None)
    if stderr:
        record["stderr_tail"] = text(
            _tail(stderr, STDERR_TAIL), STDERR_TAIL + 20
        )
    return record


def failure_reason(record):
    """One terse line for owners: 'Triage failed: <reason>'."""
    parts = [record["kind"], record["exception"]]
    if record.get("message"):
        parts.append(record["message"])
    if record.get("exit_code") is not None:
        parts.append(f"exit {record['exit_code']}")
    return "Triage failed: " + terse(" ".join(parts), 30)


def _clip_words(value, limit):
    if not isinstance(value, str):
        return value
    pieces = value.split()
    return value if len(pieces) <= limit else " ".join(pieces[:limit])


def app_only(advice):
    """Pull-request items have no framework fix to file: force app locus."""
    if not isinstance(advice, dict):
        return advice
    options = []
    for option in advice.get("options") or []:
        if isinstance(option, dict):
            option = {
                **option,
                "locus": "app",
                "fix": "",
                "upstream": None,
            }
        options.append(option)
    return {**advice, "options": options}


def clip_advice(advice):
    """Shorten over-long prose to the advisor caps instead of rejecting it.

    Long issues make the model write long rationales; the caps exist for
    terse display, so clipping keeps a useful decision where strict
    validation used to drop the whole triage into the fallback.
    """
    if not isinstance(advice, dict):
        return advice
    clipped = dict(advice)
    clipped["problem"] = _clip_words(clipped.get("problem"), PROBLEM_WORDS)
    clipped["why"] = _clip_words(clipped.get("why"), WHY_WORDS)
    if isinstance(clipped.get("details"), list):
        clipped["details"] = [
            _clip_words(d, DETAIL_WORDS) for d in clipped["details"][:6]
        ]
    options = []
    for option in clipped.get("options") or []:
        if isinstance(option, dict):
            option = {
                **option,
                "label": _clip_words(option.get("label"), LABEL_WORDS),
                "effect": _clip_words(option.get("effect"), EFFECT_WORDS),
                "tradeoff": _clip_words(option.get("tradeoff"), TRADEOFF_WORDS),
            }
            if option.get("fix"):
                option["fix"] = _clip_words(option["fix"], FIX_WORDS)
        options.append(option)
    clipped["options"] = options
    return clipped


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
    def __init__(self, home: Path, config, runner=None, sleep=time.sleep):
        self.home = Path(home)
        self.config = config
        self.runner = runner
        self.sleep = sleep
        self.last_failure = None

    def advise(self, packet: dict, *, pull_request=False):
        """(Advice, source): source is 'model' or 'fallback'.

        On fallback, ``last_failure`` holds a bounded account of why. Over-long
        prose is clipped to the caps, transient failures (timeout, rate limit,
        harness crash) are retried with backoff, schema errors are retried
        with the violation named.
        """
        reason = packet.get("block_reason", "")
        self.last_failure = None
        if self.runner is None and self.config.coordinator == "rules":
            return fallback_advice(
                reason, pull_request=pull_request
            ), "fallback"
        runner = self.runner or self._model
        prompt = advisor_prompt(self.home)
        problem = None
        contract = transient = attempts = 0
        while True:
            attempts += 1
            payload = dict(packet)
            if pull_request:
                payload["item_kind"] = (
                    "pull_request: every option has locus 'app'; the owner "
                    "reviews, merges or re-checks; no framework fixes"
                )
            if problem:
                payload["contract_error"] = {
                    "message": "Previous reply violated the schema. Fix it.",
                    "problems": problem,
                }
            try:
                reply = runner(prompt, json.dumps(payload))
                if isinstance(reply, str):
                    reply = json.loads(reply)
                if isinstance(reply, dict):
                    reply = clip_advice(reply)
                    if pull_request:
                        reply = app_only(reply)
                if not isinstance(reply, Advice):
                    reply = Advice.model_validate(reply)
                self._known_libraries(reply, packet)
                self.last_failure = None
                return reply, "model"
            except Exception as error:  # noqa: BLE001 - recorded, not lost
                self.last_failure = failure_record(error, attempts)
                kind = self.last_failure["kind"]
                if kind == "schema":
                    problem = str(error)[:1500]
                    contract += 1
                    if contract > ADVISOR_RETRIES:
                        break
                elif kind in TRANSIENT and transient < TRANSIENT_RETRIES:
                    self.sleep(BACKOFF_SECONDS[transient])
                    transient += 1
                else:
                    break
        self.last_error = self.last_failure["exception"]
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
        return structured.run(
            self.config,
            self.home,
            prompt=prompt,
            payload=payload,
            output_type=Advice,
            sources=self.sources(),
            model=self._model_name(),
            reasoning=self.config.advisor.reasoning,
        )

    def claude_options(self, prompt, sources):
        """SDK options: read-only tools confined to the framework sources."""
        return structured.claude_options(
            self.config,
            self.home,
            prompt,
            sources,
            self._model_name(),
            self.config.advisor.reasoning,
            Advice.model_json_schema(),
        )
