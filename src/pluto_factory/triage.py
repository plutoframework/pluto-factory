"""Optional triage of new issues on the receiving factory.

Issue text is untrusted. It is truncated and handed to a read-only model as
quoted JSON data; the model's reply must match a closed schema. Only a
triage-authored spec ever becomes a work instruction, and only after the
author is trusted (``trusted_authors`` or a repository collaborator with
write access) or the owner has answered a decision about the issue.

Spec delivery: a marked issue comment (the issue body is never edited). The
ledger row, not the comment, is what intake trusts: anyone can post a marker.
Edit detection: the version is a digest of title+body, so the factory's own
comments and labels (which bump ``updated_at``) never retrigger triage;
``updated_at`` is recorded for audit.
"""

import contextlib
import json
import time
from types import SimpleNamespace
from typing import Literal

from pydantic import Field, field_validator, model_validator

from . import attention, structured
from .advisor import (
    DETAIL_WORDS,
    EFFECT_WORDS,
    FIX_WORDS,
    LABEL_WORDS,
    LOCUS,
    PROBLEM_WORDS,
    TRADEOFF_WORDS,
    WHY_WORDS,
    Advice,
    fallback_advice,
    terse,
    words,
)
from .config import digest, triage_prompt
from .dependencies import REFERENCE
from .github import GitHubFailure
from .models import Contract

TITLE_LIMIT = 300
BODY_LIMIT = 6000
APPROVE = "Approve triaged spec"
REJECT = "Reject issue"
MORE = "Needs more detail; re-triage"
SPEC_MARKER = "<!-- pluto-factory:triage-spec:{version} -->"
REJECT_MARKER = "<!-- pluto-factory:triage-reject:{version} -->"
WRITE_PERMISSIONS = {"admin", "maintain", "write"}


class Spec(Contract):
    title: str
    scope: str
    acceptance: list[str] = Field(min_length=1, max_length=10)
    depends: list[str] = Field(default_factory=list, max_length=10)
    locus: str = "app"

    @field_validator("title")
    @classmethod
    def _title(cls, value):
        value = value.strip()
        if not value or words(value) > 15 or "\n" in value:
            raise ValueError("title must be one line of at most 15 words")
        return value

    @field_validator("scope")
    @classmethod
    def _scope(cls, value):
        if not value.strip() or words(value) > 150:
            raise ValueError("scope must be 1-150 words")
        return value.strip()

    @field_validator("acceptance")
    @classmethod
    def _acceptance(cls, values):
        for item in values:
            if not item.strip() or words(item) > 40:
                raise ValueError("each acceptance item is 1-40 words")
        return [item.strip() for item in values]

    @field_validator("depends")
    @classmethod
    def _depends(cls, values):
        for item in values:
            if not REFERENCE.fullmatch(item.strip()):
                raise ValueError("depends entries are #N or owner/repo#N")
        return [item.strip() for item in values]

    @field_validator("locus")
    @classmethod
    def _locus(cls, value):
        if not LOCUS.fullmatch(value.strip()):
            raise ValueError("locus must be 'app' or 'framework:<library>'")
        return value.strip()


class Rejection(Contract):
    category: Literal["duplicate", "invalid", "out_of_scope"]
    reason: str

    @field_validator("reason")
    @classmethod
    def _reason(cls, value):
        value = value.strip()
        if not value or words(value) > 40 or "\n" in value:
            raise ValueError("reason is one terse line of at most 40 words")
        return value


def _clip_words(value, limit):
    if not isinstance(value, str):
        return value
    pieces = value.split()
    return value if len(pieces) <= limit else " ".join(pieces[:limit])


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


class TriageResult(Contract):
    outcome: Literal["ACCEPT", "NEEDS_HUMAN", "REJECT"]
    spec: Spec | None = None
    advice: Advice | None = None
    rejection: Rejection | None = None

    @model_validator(mode="before")
    @classmethod
    def _lenient_advice(cls, data):
        if isinstance(data, dict) and data.get("outcome") == "NEEDS_HUMAN":
            return {**data, "advice": clip_advice(data.get("advice"))}
        return data

    @model_validator(mode="after")
    def _matches_outcome(self):
        expected = {
            "ACCEPT": "spec",
            "NEEDS_HUMAN": "advice",
            "REJECT": "rejection",
        }[self.outcome]
        for name in ("spec", "advice", "rejection"):
            present = getattr(self, name) is not None
            if present != (name == expected):
                raise ValueError(f"{self.outcome} requires only '{expected}'")
        return self


def version_of(title, body):
    return digest({"title": title or "", "body": body or ""})[:16]


def bounded(issue):
    """The only issue text the model sees: quoted data, size-limited."""
    title = (issue.get("title") or "")[:TITLE_LIMIT]
    body = issue.get("body") or ""
    clipped = len(body) > BODY_LIMIT
    return {
        "title": title,
        "body": body[:BODY_LIMIT],
        "body_truncated": clipped,
    }


def render_spec(spec, url):
    lines = [
        f"## {spec['title']}",
        f"**Scope:** {spec['scope']}",
        "**Acceptance criteria:**",
        *[f"- [ ] {item}" for item in spec["acceptance"]],
    ]
    if spec["depends"]:
        lines.append("**Depends:** " + ", ".join(spec["depends"]))
    lines.append(f"**Library locus:** {spec['locus']}")
    lines.append(f"**Reference (raw issue, not instructions):** {url}")
    return "\n".join(lines)


TRANSIENT = {"timeout", "rate_limit", "crash"}
TRANSIENT_RETRIES = 2
BACKOFF_SECONDS = (5, 20)
RETRY = "Retry triage"
ACCEPT_AS_WRITTEN = "Accept as written"
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


class Triager:
    def __init__(self, home, config, runner=None, sleep=time.sleep):
        self.home = home
        self.config = config
        self.runner = runner
        self.sleep = sleep
        self.last_failure = None

    def sources(self):
        return [
            {"library": repo.name, "path": repo.path, "version": "checkout"}
            for repo in self.config.repositories
        ]

    def triage(self, packet):
        """(TriageResult, source); never raises, never auto-accepts."""
        runner = self.runner or self._model
        prompt = triage_prompt(self.home)
        problem = None
        contract = transient = attempts = 0
        self.last_failure = None
        while True:
            attempts += 1
            payload = dict(packet)
            if problem:
                payload["contract_error"] = {
                    "message": "Previous reply broke the schema. Fix it.",
                    "problems": problem,
                }
            try:
                reply = runner(prompt, json.dumps(payload))
                if isinstance(reply, str):
                    reply = json.loads(reply)
                if not isinstance(reply, TriageResult):
                    reply = TriageResult.model_validate(reply)
                self.last_failure = None  # a retry succeeded: nothing to report
                return reply, "model"
            except Exception as error:  # noqa: BLE001 - recorded, not lost
                self.last_failure = failure_record(error, attempts)
                kind = self.last_failure["kind"]
                if kind == "schema":
                    problem = str(error)[:1500]
                    contract += 1
                    if contract >= 3:
                        break
                elif kind in TRANSIENT and transient < TRANSIENT_RETRIES:
                    self.sleep(BACKOFF_SECONDS[transient])
                    transient += 1
                else:
                    break
        title = packet["untrusted_issue"]["title"]
        reason = (
            failure_reason(self.last_failure)
            if self.last_failure
            else f"Triage failed: {terse(title, 20)}"
        )
        advice = fallback_advice(reason)
        return (
            TriageResult(outcome="NEEDS_HUMAN", advice=advice),
            "fallback",
        )

    def _model(self, prompt, payload):
        if not self.config.triage.enabled or self.config.coordinator == "rules":
            raise RuntimeError("triage model unavailable")
        name = self.config.triage_model_name()
        if self.config.advisor_provider == "anthropic":
            name = name.removeprefix("anthropic:")
        return structured.run(
            self.config,
            self.home,
            prompt=prompt,
            payload=payload,
            output_type=TriageResult,
            sources=self.sources(),
            model=name,
            reasoning=self.config.triage.reasoning,
        )


class TriageRunner:
    def __init__(self, state, adapter, config, triager):
        self.state = state
        self.adapter = adapter
        self.config = config
        self.triager = triager
        self._writers = {}

    # -- polling ---------------------------------------------------------

    def poll(self, *, force=False):
        policy = self.config.triage
        if not policy.enabled:
            return 0
        row = self.state.db.execute(
            "SELECT value FROM settings WHERE key='triage_poll_at'"
        ).fetchone()
        if not force and row and float(row["value"]) > time.time():
            return 0
        with self.state.db:
            self.state.db.execute(
                "INSERT OR REPLACE INTO settings VALUES ('triage_poll_at',?)",
                (str(time.time() + self.config.github_poll_seconds),),
            )
        handled = 0
        for slug in sorted(
            {r.github for r in self.config.repositories if r.github}
        ):
            try:
                issues = self.adapter._api_list(
                    f"repos/{slug}/issues",
                    {"state": "open", "sort": "created", "direction": "asc"},
                )
            except GitHubFailure as error:
                self.state.event(
                    "triage.poll_failed",
                    "factory",
                    {"slug": slug, "code": error.code},
                    f"triage-poll-failed:{slug}:{int(time.time() // 3600)}",
                )
                continue
            for issue in issues:
                if self._eligible(issue):
                    try:
                        handled += self.process(slug, issue)
                    except GitHubFailure as error:
                        self.state.event(
                            "triage.failed",
                            "factory",
                            {
                                "slug": slug,
                                "number": issue["number"],
                                "code": error.code,
                            },
                            f"triage-failed:{slug}#{issue['number']}:{int(time.time() // 600)}",
                        )
        return handled

    def _eligible(self, issue):
        if "pull_request" in issue or not isinstance(issue.get("number"), int):
            return False
        labels = {
            (label.get("name") if isinstance(label, dict) else label)
            for label in issue.get("labels") or []
        }
        policy = self.config.triage
        if self.config.github_issue_label in labels:
            return False
        if policy.reject_label in labels:
            return False
        return not policy.watch_label or policy.watch_label in labels

    def _row(self, slug, number):
        return self.state.db.execute(
            "SELECT * FROM triage_issues WHERE slug=? AND number=?",
            (slug, number),
        ).fetchone()

    # -- one issue --------------------------------------------------------

    def process(self, slug, issue):
        number = issue["number"]
        title, body = issue.get("title") or "", issue.get("body") or ""
        version = version_of(title, body)
        row = self._row(slug, number)
        if row and row["version"] == version:
            if row["state"] == "accepting":
                self._deliver(slug, number, row)  # resume after a crash
                return 1
            if row["state"] != "retriage":
                return 0  # this exact issue version was already triaged
        author = ((issue.get("user") or {}).get("login") or "").strip()
        trusted = self._trusted(slug, author)
        guidance = row["guidance"] if row and row["version"] == version else ""
        packet = {
            "repository": slug,
            "untrusted_issue": bounded(issue),
            "human_guidance": guidance,
        }
        result, source = self.triager.triage(packet)
        now = time.time()
        with self.state.db:
            self.state.db.execute(
                "INSERT INTO triage_issues (slug,number,version,updated_at,"
                "state,author,trusted,guidance,created,updated) "
                "VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(slug,number) DO "
                "UPDATE SET version=excluded.version,updated_at="
                "excluded.updated_at,state=excluded.state,author="
                "excluded.author,trusted=excluded.trusted,"
                "guidance=excluded.guidance,spec=NULL,decision_id=NULL,"
                "updated=excluded.updated",
                (
                    slug,
                    number,
                    version,
                    issue.get("updated_at") or "",
                    "triaged",
                    author,
                    int(trusted),
                    guidance,
                    now,
                    now,
                ),
            )
            self.state._event(
                "triage.decided",
                "factory",
                {
                    "issue": f"{slug}#{number}",
                    "outcome": result.outcome,
                    "source": source,
                    "trusted": trusted,
                    **(
                        {"failure": self.triager.last_failure}
                        if source == "fallback"
                        and getattr(self.triager, "last_failure", None)
                        else {}
                    ),
                },
            )
        owner_engaged = bool(guidance)
        if result.outcome == "ACCEPT":
            if trusted or owner_engaged:
                self._begin_accept(slug, number, version, result.spec)
            else:
                self._cap(slug, number, version, issue, author, result.spec)
        elif result.outcome == "REJECT":
            self._reject(slug, number, version, result.rejection.reason)
        elif source == "fallback":
            self._failed(slug, number, version, issue, trusted)
        else:
            self._needs_human(slug, number, version, issue, result.advice)
        return 1

    def _trusted(self, slug, login):
        if not login:
            return False
        if login.lower() in {
            a.lower() for a in self.config.triage.trusted_authors
        }:
            return True
        key = (slug, login.lower())
        if key not in self._writers:
            try:
                permission = self.adapter._json(
                    [
                        "gh",
                        "api",
                        f"repos/{slug}/collaborators/{login}/permission",
                    ]
                ).get("permission")
            except GitHubFailure:
                permission = None
            self._writers[key] = permission in WRITE_PERMISSIONS
        return self._writers[key]

    # -- outcomes ---------------------------------------------------------

    def _decision(
        self,
        slug,
        number,
        version,
        issue,
        advice,
        lead="",
        advice_kind="triage",
    ):
        evidence = bounded(issue)
        decision = attention.add_decision(
            self.state,
            question=advice.problem,
            kind="free_text",
            details=advice.details,
            why=advice.why,
            context=(
                (f"{lead}\n" if lead else "")
                + f"{slug}#{number} by {(issue.get('user') or {}).get('login')}"
                f"\n{evidence['title']}\n\n{evidence['body']}"
            ),
            options=[
                {
                    "label": o.label,
                    "effect": o.effect,
                    "tradeoff": o.tradeoff,
                    "locus": o.locus,
                    "fix": o.fix,
                    "description": o.effect,
                    "recommended": o.recommended,
                }
                for o in advice.options
            ],
            advice=advice_kind,
            block_key=f"triage:{slug}#{number}:{version}",
            proposal={"triage": f"{slug}#{number}", "version": version},
        )
        self._set(
            slug, number, state="awaiting_human", decision_id=decision["id"]
        )
        return decision

    def _failed(self, slug, number, version, issue, trusted):
        """Model triage failed: one meaningful option set, with the reason."""
        failure = getattr(self.triager, "last_failure", None)
        reason = (
            failure_reason(failure)
            if failure
            else "Triage failed: reason unrecorded"
        )
        options = [
            {
                "label": RETRY,
                "effect": "Run triage again on this issue.",
                "tradeoff": "May fail the same way.",
                "recommended": True,
            }
        ]
        if trusted:
            options.append(
                {
                    "label": ACCEPT_AS_WRITTEN,
                    "effect": "Label ready; work follows the issue text.",
                    "tradeoff": "No triage review of scope.",
                }
            )
            evidence = bounded(issue)
            self._set(
                slug,
                number,
                spec=json.dumps(
                    {
                        "title": terse(evidence["title"], 15),
                        "scope": terse(
                            evidence["body"] or evidence["title"], 150
                        ),
                        "acceptance": ["Resolve the issue as described."],
                        "depends": [],
                        "locus": "app",
                    }
                ),
            )
        details = []
        if failure:
            details = [
                _tail(f"{k}: {v}", 140)
                for k, v in failure.items()
                if k in {"kind", "exit_code", "stderr_tail", "cause"}
            ][:4]
        # One option is legal here (the advisor schema needs two).
        advice = SimpleNamespace(
            problem=reason,
            details=details,
            why="Triage failed; retrying is cheapest.",
            options=[
                SimpleNamespace(
                    locus="app", fix="", **{"recommended": False, **option}
                )
                for option in options
            ],
        )
        self._decision(slug, number, version, issue, advice, lead=reason)

    def _needs_human(self, slug, number, version, issue, advice):
        self._decision(slug, number, version, issue, advice)

    def _cap(self, slug, number, version, issue, author, spec):
        """Untrusted author: triage may not accept; the owner decides."""
        advice = Advice.model_validate(
            {
                "problem": terse(
                    f"Triage would accept '{spec.title}'; author {author or 'unknown'} untrusted.",
                    40,
                ),
                "details": [
                    f"Author {author or 'unknown'}: not trusted or collaborator."
                ],
                "options": [
                    {
                        "label": APPROVE,
                        "effect": "Label ready; work follows the triaged spec.",
                        "tradeoff": "Trusts an unvetted author's request.",
                        "recommended": True,
                    },
                    {
                        "label": REJECT,
                        "effect": "Comment and label rejected; issue stays open.",
                        "tradeoff": "Request is dropped.",
                    },
                    {
                        "label": MORE,
                        "effect": "Re-triage with your note as guidance.",
                        "tradeoff": "Costs another triage run.",
                    },
                ],
                "why": "Triage found it valid and in scope.",
            }
        )
        self._set(slug, number, spec=json.dumps(spec.model_dump()))
        self._decision(slug, number, version, issue, advice)

    def _set(self, slug, number, **fields):
        assignments = ",".join(f"{key}=?" for key in fields)
        with self.state.db:
            self.state.db.execute(
                f"UPDATE triage_issues SET {assignments},updated=? "
                "WHERE slug=? AND number=?",
                (*fields.values(), time.time(), slug, number),
            )

    # -- accept -----------------------------------------------------------

    def _begin_accept(self, slug, number, version, spec):
        self._set(
            slug,
            number,
            state="accepting",
            spec=json.dumps(
                spec.model_dump() if hasattr(spec, "model_dump") else spec
            ),
        )
        self._deliver(slug, number, self._row(slug, number))

    def _deliver(self, slug, number, row):
        spec = json.loads(row["spec"])
        url = f"https://github.com/{slug}/issues/{number}"
        marker = SPEC_MARKER.format(version=row["version"])
        self._comment_once(slug, number, marker, render_spec(spec, url))
        self.adapter._run(
            [
                "gh",
                "issue",
                "edit",
                str(number),
                "--repo",
                slug,
                "--add-label",
                self.config.github_issue_label,
            ],
            mutating=True,
        )
        self._set(slug, number, state="accepted")
        self.state.event(
            "triage.accepted", "factory", {"issue": f"{slug}#{number}"}
        )

    def _comment_once(self, slug, number, marker, text):
        existing = self.adapter._json(
            [
                "gh",
                "issue",
                "view",
                str(number),
                "--repo",
                slug,
                "--json",
                "comments",
            ]
        )
        if any(
            marker in (c.get("body") or "")
            for c in existing.get("comments") or []
        ):
            return False
        self.adapter._run(
            [
                "gh",
                "issue",
                "comment",
                str(number),
                "--repo",
                slug,
                "--body",
                f"{marker}\n{text}",
            ],
            mutating=True,
        )
        return True

    # -- reject -----------------------------------------------------------

    def _reject(self, slug, number, version, reason, category=None):
        label = self.config.triage.reject_label
        marker = REJECT_MARKER.format(version=version)
        self._comment_once(
            slug, number, marker, f"**Not taken up:** {terse(reason, 40)}"
        )
        self.adapter._run(
            [
                "gh",
                "label",
                "create",
                label,
                "--repo",
                slug,
                "--description",
                "Rejected by factory triage; the issue stays open",
                "--force",
            ],
            mutating=True,
            acceptable=(0, 1),
        )
        self.adapter._run(
            [
                "gh",
                "issue",
                "edit",
                str(number),
                "--repo",
                slug,
                "--add-label",
                label,
            ],
            mutating=True,
        )
        self._set(slug, number, state="rejected")
        self.state.event(
            "triage.rejected", "factory", {"issue": f"{slug}#{number}"}
        )

    # -- owner answers ----------------------------------------------------

    def apply_answer(self, decision):
        slug, _, raw = decision["proposal"]["triage"].rpartition("#")
        number = int(raw)
        row = self._row(slug, number)
        if row is None or row["version"] != decision["proposal"]["version"]:
            return  # the issue was edited; the new version is triaged anew
        answer = decision["answer"]
        if answer == RETRY:
            self._set(slug, number, state="retriage", guidance="")
        elif answer in {APPROVE, ACCEPT_AS_WRITTEN} and row["spec"]:
            self._begin_accept(
                slug, number, row["version"], json.loads(row["spec"])
            )
        elif answer == REJECT:
            self._reject(slug, number, row["version"], "Rejected by the owner.")
        else:
            self._set(
                slug,
                number,
                state="retriage",
                guidance=attention.guidance_text(decision),
            )

    # -- intake -----------------------------------------------------------

    def accepted_spec(self, slug, number):
        row = self._row(slug, number)
        if row and row["state"] == "accepted" and row["spec"]:
            return json.loads(row["spec"])
        return None
