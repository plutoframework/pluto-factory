"""Durable phase transitions around interchangeable interactive workers."""

import json
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextlib import nullcontext
from pathlib import Path
from threading import Event

from pydantic import ValidationError

from .acceptance import (
    AcceptanceContractError,
    acceptance_intent,
    citable_ids,
    current_evidence,
)
from .advisor import Advisor, evidence_packet, fallback_advice, terse
from .attention import (
    GAP_RESUME,
    add_decision,
    answer_decision,
    apply_advice,
    apply_config_change,
    apply_due_autos,
    approved,
    auto_verdict,
    chosen_option,
    enqueue_issue_comment,
    flush_issue_comments,
    framework_library,
    get_decision,
    get_pr,
    guidance_text,
    list_decisions,
    mark_applied,
    record_framework_gap,
    set_auto_pending,
    set_pr,
    withdraw_decision,
)
from .config import (
    atomic_json,
    digest,
    load_config,
    resolved_bundle,
    role_prompt,
)
from .coordinator import Coordinator, CoordinatorError
from .framework_sources import pins
from .git import GitFailure, GitWorkspace
from .github import GitHubFailure
from .harness import HarnessError, make_harness
from .improvements import propose
from .models import (
    WRITER_ROLES,
    Failure,
    WorkerReport,
    workable_repositories,
)
from .notifications import flush
from .post_merge import confirm_merged, reassess_merged, revalidate_merged
from .pr_tracker import (
    REPAIR_RULES,
    REVIEW_CHECKLIST,
    PRTracker,
    parse_repair_source,
)
from .propagation import propagate
from .ratelimit import transcript_failure
from .recovery import plan_recovery
from .scheduler import Scheduler
from .state import State, StateConflict, identity
from .trace import Tracer
from .triage import Triager, TriageRunner
from .upstream import (
    apply_hold_answer,
    fixed_guidance,
    flush_upstream,
    poll_external,
    request_upstream,
)

# Sweeping Core for orphaned worker terminals lists every terminal, so it runs
# on coordinator start and then at this interval rather than on every tick.
REAP_INTERVAL_SECONDS = 300
# Times the coordinator is re-asked with the validation error before deferring.
CONTRACT_RETRIES = 2
GUIDANCE_AUTHORITY = (
    "AUTHORITATIVE human decision. It overrides prior conclusions, "
    "reports and deferrals. Follow it, or report precisely why it cannot "
    "be followed; do not defer again for the question it answers."
)


MERGE_MANAGER_RULES = (
    "Preserve BOTH intents: this item's change and every merged change in "
    "the packet. Never drop the other side's behavior. Never weaken, "
    "delete, skip or loosen tests to resolve a conflict. Regenerate "
    "generated files with the repository's generator instead of "
    "hand-merging them, where applicable. Change nothing unrelated to the "
    "conflict. Remove every conflict marker, then run the repository "
    "checks with factory_check and cite the result."
)
CONFLICT_REVIEW_CHECKLIST = (
    "This candidate contains a conflict resolution against the moving "
    "default branch. Beyond the normal review: (1) confirm no conflict "
    "marker remains; (2) confirm the incoming (merged) side's behavior is "
    "fully preserved, not silently dropped; (3) confirm this item's own "
    "behavior is still present; (4) confirm no test was weakened, deleted "
    "or skipped to resolve the conflict; (5) confirm generated files were "
    "regenerated, not hand-edited; (6) report any lost or duplicated "
    "logic, and any change unrelated to the conflict, as required findings."
)


def merge_attempt_key(data):
    """Merge attempts are budgeted per published PR head."""
    heads = sorted(
        str(pr.get("head") or "")
        for pr in data.get("pull_requests", [])
        if pr.get("state") != "merged"
    )
    return ",".join(heads) or "unpublished"


def merge_attempt_number(data):
    return (data.get("merge_attempts") or {}).get(merge_attempt_key(data), 0)


def orphaned_terminals(terminals, work_items, factory):
    """Owned terminals whose assignment is not a current, unfinished run.

    Terminals lacking this factory's ``factory`` and ``assignment`` labels are
    never considered, whatever the adapter returned.
    """
    active = {
        work["current_run"]
        for work in work_items
        if work["phase"] != "completed" and work["current_run"]
    }
    return [
        terminal
        for terminal in terminals
        if terminal.get("labels", {}).get("factory") == factory
        and terminal.get("labels", {}).get("assignment")
        and terminal["labels"]["assignment"] not in active
    ]


EVIDENCE_FULL = 3
EVIDENCE_TEXT_LIMIT = 2000
EVIDENCE_SUMMARY_KEYS = (
    "kind",
    "passed",
    "exit_code",
    "command",
    "repository",
    "seconds",
    "timed_out",
    "artifact",
)


def _clip(text, limit=EVIDENCE_TEXT_LIMIT):
    if not isinstance(text, str) or len(text) <= limit:
        return text
    return f"[{len(text) - limit} earlier chars omitted]\n" + text[-limit:]


def bound_evidence(items):
    """Keep the newest evidence in full and older rows as pointers.

    Items arrive newest first. Full rows clip large output text to its tail;
    older rows keep only identifying fields plus their ``artifact`` path, so
    the packet stops growing with every revision.
    """
    bounded = []
    for index, item in enumerate(items):
        if index < EVIDENCE_FULL:
            row = {
                key: _clip(value) if key in {"stdout", "stderr"} else value
                for key, value in item.items()
            }
        else:
            row = {k: item[k] for k in EVIDENCE_SUMMARY_KEYS if k in item}
            if "report" in item:
                report = item["report"]
                row["report_summary"] = (
                    {k: report[k] for k in ("status", "summary") if k in report}
                    if isinstance(report, dict)
                    else None
                )
            row["summarized"] = (
                "Older evidence; full output is in the artifact file."
            )
        bounded.append(row)
    return bounded


class Engine:
    def __init__(self, home: Path, *, harness=None, coordinator=None):
        self.home = Path(home).resolve()
        self.config = load_config(self.home)
        self.state = State(self.home)
        self.git = GitWorkspace(self.home, self.config)
        self.harness = harness or make_harness(self.config)
        self.coordinator = coordinator or Coordinator(self.home, self.config)
        self.tracer = Tracer(self.home, self.config)
        if self.tracer.enabled:
            self.state.tracer = self.tracer
        self._reaped_at = None
        self.scheduler = Scheduler(self.config)
        # Parallel steps: work id -> (future, phase of the step in flight).
        self._executor = None
        self._running = {}
        self._skips = {}
        # Coordinator model calls are serialized unless the config allows
        # parallel decisions; each decision reads only its own item.
        self._decision_lock = threading.Lock()
        self.github = None
        if self.config.integration == "github":
            from .github_flow import GitHubFlow

            self.github = GitHubFlow(self.state, self.config)
        self.tracker = PRTracker(self)
        self.advisor = Advisor(self.home, self.config)
        self.triage = TriageRunner(
            self.state,
            self.tracker.adapter,
            self.config,
            Triager(self.home, self.config),
        )

    def _ready(self, work):
        if work["phase"] == "completed" or work["control"] in {
            "paused",
            "deferred",
            "quarantined",
        }:
            return False
        if work["wake_at"] > time.time():
            return False
        return self.state.dependency_status(work["id"])["ready"]

    def _limited(self):
        """True while a provider limit holds back new dispatches."""
        until = self.state.rate_limited_until()
        if not until:
            return False
        if until > time.time():
            return True
        self.state.clear_rate_limit()
        return False

    def tick(self):
        if self.state.is_paused():
            return False
        if not self.config.concurrency.parallel:
            return self._tick_serial()
        return self._tick_parallel()

    def _tick_serial(self):
        work_items = self.state.list_work()
        active = [
            work
            for work in work_items
            if self._ready(work) and work["current_run"]
        ]
        if self._limited():
            ready = active
        else:
            ready = active or [work for work in work_items if self._ready(work)]
        if not ready:
            return False
        self._guarded_step(ready[0])
        return True

    def _guarded_step(self, work):
        try:
            self._step(work)
        except StateConflict:
            self.state.event("operation.superseded", work["id"], {})
        except CoordinatorError as error:
            self._failure(work["id"], error.failure)
        except HarnessError as error:
            self._failure(work["id"], Failure.model_validate(error.failure))
        except (GitFailure, GitHubFailure) as error:
            if self._conflict_revision(work, error):
                return
            self._failure(
                work["id"],
                Failure(
                    code=error.code,
                    summary=error.summary,
                    source="adapter",
                    uncertain=error.uncertain,
                ),
            )
        except (ValueError, ValidationError) as error:
            self._failure(
                work["id"],
                Failure(
                    code="contract.invalid",
                    summary=str(error)[:2000],
                    source="coordinator",
                ),
            )

    # -- parallel workers ------------------------------------------------

    def _tick_parallel(self):
        """Step every live run, then admit new work up to the limits.

        Steps run on pool threads so a long check or a slow launch for one
        item never delays observing the others. At most one step per work
        item is in flight; ``scheduler.admit`` gates everything that would
        occupy a new slot, repository, phase or host resource.
        """
        finished = self._reap_steps()
        work_items = self.state.list_work()
        running = {wid: phase for wid, (_, phase) in self._running.items()}
        ready = [
            work
            for work in work_items
            if work["id"] not in running and self._ready(work)
        ]
        submitted = 0
        for work in ready:
            if work["current_run"]:
                self._submit(work)
                submitted += 1
        if not self._limited():
            for work in ready:
                if work["current_run"]:
                    continue
                verdict = self.scheduler.admit(work, work_items, running)
                if not verdict.admitted:
                    self._note_skip(work, verdict.reason)
                    continue
                self._skips.pop(work["id"], None)
                if work["phase"] == "queued":
                    self.scheduler.note_start()
                running[work["id"]] = work["phase"]
                self._submit(work)
                submitted += 1
        return bool(submitted or finished)

    def _note_skip(self, work, reason):
        """Record why a ready item waits, once per distinct reason."""
        if self._skips.get(work["id"]) == reason:
            return
        self._skips[work["id"]] = reason
        self.state.event(
            "dispatch.deferred",
            work["id"],
            {"phase": work["phase"], "reason": reason},
        )

    def _submit(self, work):
        if self._executor is None:
            size = self.config.concurrency.max_workers * 2 + 2
            self._executor = ThreadPoolExecutor(
                max_workers=size, thread_name_prefix="factory-step"
            )
        future = self._executor.submit(self._guarded_step, work)
        self._running[work["id"]] = (future, work["phase"])

    def _reap_steps(self):
        """Collect finished steps; an unexpected error stops the factory."""
        done = [
            wid for wid, (future, _) in self._running.items() if future.done()
        ]
        for wid in done:
            future, _ = self._running.pop(wid)
            future.result()
        return len(done)

    def drain(self):
        """Wait for every in-flight step to finish."""
        while self._running:
            wait(
                [future for future, _ in self._running.values()],
                return_when=FIRST_COMPLETED,
            )
            self._reap_steps()

    def active_runs(self):
        """Work items currently holding a worker slot."""
        return self.scheduler.active(
            self.state.list_work(),
            {wid: phase for wid, (_, phase) in self._running.items()},
        )

    def run(self, once=False, max_seconds=None, watch=None):
        started = time.monotonic()
        watching = (
            bool(self.github or self.config.tracked_repositories)
            if watch is None
            else watch
        )
        with self.state.writer():
            self.state.event(
                "coordinator.started",
                "factory",
                {
                    "harness": self.config.harness,
                    "coordinator": self.config.coordinator,
                },
            )
            self.reap_sessions()
            while not self.state.is_paused():
                if max_seconds is not None and (
                    time.monotonic() - started >= max_seconds
                ):
                    break
                if time.monotonic() - self._reaped_at >= REAP_INTERVAL_SECONDS:
                    self.reap_sessions()
                self.attention(force=once)
                if self.github:
                    self.github.poll(force=once)
                progressed = self.tick()
                if once:
                    self.drain()
                    break
                work = self.state.list_work()
                pending = [
                    item
                    for item in work
                    if item["phase"] != "completed"
                    and item["control"]
                    not in {
                        "deferred",
                        "paused",
                        "quarantined",
                    }
                ]
                if not pending and not watching:
                    break
                if not progressed and self._running:
                    wait(
                        [f for f, _ in self._running.values()],
                        timeout=1,
                        return_when=FIRST_COMPLETED,
                    )
                    continue
                if not progressed:
                    future = [
                        item["wake_at"]
                        for item in pending
                        if item["wake_at"] > time.time()
                    ]
                    until = self.state.rate_limited_until()
                    if until > time.time():
                        future.append(until)
                    if not future and not watching:
                        break
                    delay = (
                        max(min(future) - time.time(), 0.01) if future else 1
                    )
                    Event().wait(min(delay, 1))
            self.drain()
            self.flush_notifications()
        return self.state.inspect()

    def _step(self, work):
        if work["current_run"]:
            self._collect(work)
            return
        work_id = work["id"]
        data = work["data"]
        phase = work["phase"]
        repair = self._repair_target(work)
        if (
            phase in {"qa_checks", "qa"}
            and not repair
            and self._synchronize(work_id, data, None)
        ):
            return
        if phase == "queued" and repair:
            self._prepare_repair(work, repair)
        elif phase == "queued":
            for name in work["payload"]["repositories"]:
                repo = self.git.repositories.get(name)
                if repo is None:
                    raise GitFailure("scope.denied", "Unregistered repository")
                if repo.access == "write" and (not repo.checks or not repo.qa):
                    raise GitFailure(
                        "validation.unconfigured",
                        f"Configure required checks and QA for {name} before dispatch",
                    )
            operation = self.state.begin_operation(
                work_id, "git.prepare", work["payload"], f"prepare:{work_id}"
            )
            workspace_id = work_id
            if data.get("increment"):
                workspace_id += f"-increment-{data['increment']}"
            entries = self.git.prepare(
                workspace_id, work["payload"]["repositories"]
            )
            data["workspace_id"] = workspace_id
            if self.github:
                self.github.adapter.refresh_defaults(entries)
                entries, _ = self.git.sync(entries)
            if not any(entry["access"] == "write" for entry in entries):
                raise ValueError("work needs at least one writable repository")
            data["entries"] = entries
            self.state.finish_operation(operation, {"entries": entries})
            self.state.update_work(
                work_id,
                phase="implementing",
                control="ready",
                data=data,
                event="work.prepared",
            )
        elif phase in {"implementing", "reviewing", "qa"}:
            role = {
                "implementing": "implementer",
                "reviewing": "reviewer",
                "qa": "qa",
            }[phase]
            pending = work["data"].get("conflict_revision") or {}
            if (
                role == "implementer"
                and not repair
                and pending.get("resolver") == "merge_manager"
            ):
                role = "merge_manager"
            notes = None
            if role in WRITER_ROLES and not repair:
                notes = self._synchronize_for_dispatch(work)
                work = self.state.get_work(work["id"])
                pending = work["data"].get("conflict_revision")
                if notes is None and pending:
                    # The merge is already prepared in the worktree.
                    notes = {
                        "default_branch_merged": [],
                        "merge_conflict": pending["summary"],
                        "guidance": (
                            "This is a conflict-only revision: resolve the "
                            "prepared merge with the new default branch and "
                            "change nothing else."
                        ),
                    }
            self._dispatch(work, role, notes)
        elif phase == "checking":
            operation = self.state.begin_operation(work_id, "git.snapshot", {})
            data["entries"] = self.git.snapshot(
                data["entries"], data.get("workspace_id", work_id)
            )
            self.state.finish_operation(operation, {"entries": data["entries"]})
            # Persist the snapshot (the factory's merge commit) before
            # synchronizing: a new conflict must not lose it.
            self.state.update_work(work_id, data=data)
            if repair:
                # Judged by CI on the PR branch, not rebased; an independent
                # reviewer must clear it before anything is pushed.
                self._validate(work_id, "checks", "reviewing")
                return
            if not self._synchronize(work_id, data, None):
                self.state.update_work(work_id, data=data)
            self._validate(work_id, "checks", "reviewing")
        elif phase == "pushing" and repair:
            self._push_repair(work, repair)
        elif phase == "preparing":
            self._synchronize(work_id, data, "qa_checks")
        elif phase == "qa_checks":
            self._validate(work_id, "qa", "qa")
        elif phase == "integrating":
            self._integrate(work)
        elif phase == "awaiting_merge" and self.github:
            self.github.advance(
                work,
                self._revise,
                self._defer,
                reassess=lambda item: reassess_merged(self, item),
                confirm=lambda item: confirm_merged(self, item),
                revalidate=lambda item: revalidate_merged(self, item),
                resync=self._resync_pull_requests,
            )
        else:
            raise ValueError(f"unsupported phase {phase}")

    # -- PR repair, tracking and owner decisions -------------------------

    def _repair_target(self, work):
        """(slug, number) when this work item repairs a tracked PR."""
        return parse_repair_source(work["payload"].get("source"))

    def _prepare_repair(self, work, target):
        slug, number = target
        work_id = work["id"]
        data = work["data"]
        row = get_pr(self.state, slug, number)
        if row is None or not row["head"]:
            raise GitFailure("scope.denied", "Repair target is not tracked")
        name = work["payload"]["repositories"][0]
        repo = self.git.repositories.get(name)
        if repo is None or repo.github != slug or not repo.checks:
            raise GitFailure(
                "validation.unconfigured",
                f"Configure a checkout and checks for {slug} before repair",
            )
        operation = self.state.begin_operation(
            work_id, "git.prepare", work["payload"], f"prepare:{work_id}"
        )
        entries = self.git.prepare(work_id, [name])
        entry = entries[0]
        tip = self.tracker.adapter.fetch_pr_head(
            entry, slug, row["branch"], number
        )
        if tip != row["head"]:
            self.state.event(
                "pr.repair_stale",
                work_id,
                {"expected": row["head"], "tip": tip},
            )
            raise GitFailure(
                "git.candidate_changed", "PR head moved before repair started"
            )
        self.git._git(Path(entry["worktree"]), "reset", "--hard", tip)
        entry.update(
            base=tip,
            candidate=tip,
            tree=self.git._text(
                Path(entry["worktree"]), "rev-parse", "HEAD^{tree}"
            ),
            pr_branch=row["branch"],
        )
        self.git._save(entries)
        data["workspace_id"] = work_id
        data["entries"] = entries
        data["repair"] = {"slug": slug, "number": number, "head": tip}
        self.state.finish_operation(operation, {"entries": entries})
        self.state.update_work(
            work_id,
            phase="implementing",
            control="ready",
            data=data,
            event="work.prepared",
        )

    def _push_repair(self, work, target):
        slug, number = target
        data = work["data"]
        entry = data["entries"][0]
        repair = data["repair"]
        row = get_pr(self.state, slug, number)
        if entry["candidate"] == repair["head"]:
            self._defer(work, "The repair produced no changes to push")
            if row:
                set_pr(
                    self.state,
                    row,
                    state="needs_owner",
                    reason="Repair worker made no changes; needs a human",
                )
            return
        operation = self.state.begin_operation(
            work["id"], "github.push_repair", {"candidate": entry["candidate"]}
        )
        pushed = self.tracker.adapter.push_repair(
            entry, slug, entry["pr_branch"], repair["head"]
        )
        self.state.finish_operation(operation, {"pushed": pushed})
        if row:
            set_pr(
                self.state,
                row,
                state="pending",
                head=pushed,
                reason="Repair pushed; waiting for checks",
                failing=[],
                log_excerpt="",
            )
        self.state.update_work(
            work["id"],
            phase="completed",
            control="settled",
            data=data,
            event="work.completed",
            notice={
                "kind": "completed",
                "message": f"Repair pushed to {slug}#{number}",
                "incident_key": f"completed:{work['id']}",
            },
        )

    def attention(self, force=False):
        """Apply answered decisions, then poll tracked pull requests."""
        self.check_framework_gaps()
        apply_due_autos(self.state)
        self.process_decisions()
        self.advise_pending()
        self.flush_upstream()
        self.propagate()
        self.flush_issue_comments()
        if self.state.is_paused():
            return
        try:
            self.triage.poll(force=force)
        except GitHubFailure as error:
            self.state.event(
                "triage.poll_failed", "factory", {"code": error.code}
            )
        poll_external(
            self.state,
            self.tracker.adapter,
            self.config,
            self._upstream_fixed,
            force=force,
        )
        try:
            self.tracker.poll(force=force)
        except GitHubFailure as error:
            self.state.event("pr.poll_failed", "factory", {"code": error.code})

    def process_decisions(self):
        applied = []
        for decision in list_decisions(self.state, {"answered"}):
            try:
                self._apply_decision(decision)
            except (ValueError, OSError, StateConflict) as error:
                self.state.event(
                    "decision.apply_failed",
                    decision["work_id"] or "factory",
                    {"id": decision["id"], "error": str(error)[:500]},
                    f"decision-apply-failed:{decision['id']}:{str(error)[:60]}",
                )
                continue
            applied.append(decision["id"])
        return applied

    def _apply_decision(self, decision):
        text = guidance_text(decision)
        detail = {}
        proposal = decision["proposal"] or {}
        if decision["kind"] == "config":
            if approved(decision):
                detail["backup"] = apply_config_change(
                    self.home, proposal["key"], proposal["value"]
                )
                self._reload_config()
                text += (
                    f". factory.toml updated: {proposal['key']} = "
                    f"{proposal['value']}"
                )
            else:
                text += ". No configuration change was made."
        if proposal.get("upstream_hold"):
            apply_hold_answer(self.state, decision)
            mark_applied(self.state, decision["id"], detail)
            return
        if proposal.get("triage"):
            self.triage.apply_answer(decision)
            mark_applied(self.state, decision["id"], detail)
            return
        if (
            proposal.get("pr")
            and not decision["custom"]
            and decision["answer"].startswith("Grant")
        ):
            slug, _, number = proposal["pr"].rpartition("#")
            self.tracker.grant(slug, int(number))
        option = chosen_option(decision)
        if framework_library(option):
            # Parked until the gap decision is answered or the pin moves.
            gap = record_framework_gap(
                self.state,
                decision,
                option,
                pins(self.advisor.sources()),
            )
            detail["framework_gap"] = gap["id"]
            request_upstream(self.state, self.config, decision, option)
            enqueue_issue_comment(self.state, decision, "answered")
            mark_applied(self.state, decision["id"], detail)
            return
        if decision["work_id"]:
            work = self.state.get_work(decision["work_id"])
            self.state.add_guidance(decision["work_id"], text, "owner")
            if work["control"] in {"deferred", "quarantined", "backoff"}:
                self.state.resume(decision["work_id"])
        enqueue_issue_comment(self.state, decision, "answered")
        if proposal.get("external"):
            with self.state.db:
                self.state.db.execute(
                    "UPDATE external_issues SET state='waived' WHERE key=?",
                    (proposal["external"],),
                )
        mark_applied(self.state, decision["id"], detail)

    def _reload_config(self):
        self.config = load_config(self.home)
        self.git.config = self.config
        self.tracker.config = self.config
        self.advisor.config = self.config
        self.triage.config = self.config
        self.triage.triager.config = self.config
        if hasattr(self.coordinator, "config"):
            self.coordinator.config = self.config
        self.scheduler.config = self.config

    def _ask_owner(self, work, reason):
        """Every deferral a human must resolve becomes a durable decision.

        Filed at once with a deterministic terse headline; the advisor
        rewrites headline, options and rationale in place on a later tick.
        Raw block text stays as collapsed evidence.
        """
        advice = fallback_advice(reason)
        add_decision(
            self.state,
            work_id=work["id"],
            kind="free_text",
            notify=False,  # the deferral already raised action_required
            question=f"{work['title']}: {terse(reason, 30)}"[:500],
            context=(
                f"Work '{work['title']}' ({work['id']}), phase "
                f"{work['phase']}.\n{reason}"
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
            why=advice.why,
            advice="pending",
            block_key=digest({"work": work["id"], "reason": reason})[:24],
        )

    def check_framework_gaps(self):
        """A bumped library pin answers its open framework gap."""
        open_gaps = [
            d
            for d in list_decisions(self.state, {"open"})
            if d["kind"] == "framework_gap" and d["proposal"]
        ]
        if not open_gaps:
            return
        current = pins(self.advisor.sources())
        for gap in open_gaps:
            library, pinned = gap["proposal"]["library"], gap["proposal"]["pin"]
            now = current.get(library)
            if now and pinned != "unknown" and now != pinned:
                answer_decision(
                    self.state,
                    gap["id"],
                    GAP_RESUME,
                    f"{library} pin bumped {pinned} -> {now}",
                    via="coordinator",
                    by="coordinator",
                )

    def advise_pending(self, limit=2):
        """Run the advisor on decisions still carrying raw block text."""
        if self._limited():
            return 0
        done = 0
        rows = self.state.db.execute(
            "SELECT id FROM decisions WHERE state='open' AND "
            "advice='pending' ORDER BY created LIMIT ?",
            (limit,),
        ).fetchall()
        for row in rows:
            decision = get_decision(self.state, row["id"])
            packet = evidence_packet(self.state, decision, decision["context"])
            advice, source = self.advisor.advise(
                packet, pull_request=decision["work_id"] is None
            )
            ok, _why = (
                auto_verdict(self.config.autonomy, advice, packet=packet)
                if source == "model"
                else (False, "fallback advice")
            )
            if apply_advice(
                self.state, decision["id"], advice, source, announce=not ok
            ):
                done += 1
                if ok:
                    set_auto_pending(
                        self.state,
                        decision["id"],
                        advice,
                        self.config.autonomy.grace_seconds,
                    )
                    continue
                self._auto_upstream(get_decision(self.state, decision["id"]))
        return done

    def _auto_upstream(self, decision):
        """Under file_upstream=auto, a framework-locus recommendation files."""
        if self.config.framework.file_upstream != "auto":
            return
        option = next(
            (o for o in decision["options"] if o["recommended"]), None
        )
        if framework_library(option):
            request_upstream(self.state, self.config, decision, option)

    def propagate(self):
        try:
            return propagate(self.state, self.tracker.adapter, self.config)
        except GitHubFailure as error:
            self.state.event(
                "propagation.failed", "factory", {"code": error.code}
            )
            return 0

    def flush_upstream(self):
        try:
            return flush_upstream(self.state, self.tracker.adapter, self.config)
        except GitHubFailure as error:
            self.state.event("upstream.failed", "factory", {"code": error.code})
            return 0

    def _upstream_fixed(self, work_id, slug, number, closing, cli_revision=""):
        """An upstream issue closed as completed: resume the parked work."""
        for gap in list_decisions(self.state, {"open"}):
            if gap["kind"] == "framework_gap" and gap["work_id"] == work_id:
                withdraw_decision(self.state, gap["id"])
        work = self.state.get_work(work_id)
        self.state.add_guidance(
            work_id,
            fixed_guidance(slug, number, closing, cli_revision),
            "coordinator",
        )
        if work["control"] in {"deferred", "quarantined", "backoff"}:
            self.state.resume(work_id)

    def flush_issue_comments(self):
        allowed = {
            repo.github
            for repo in workable_repositories(self.config)
            if repo.github
        }
        try:
            return flush_issue_comments(
                self.state, self.tracker.adapter, allowed
            )
        except GitHubFailure as error:
            self.state.event(
                "issue_comment.failed", "factory", {"code": error.code}
            )
            return 0

    def _resync_pull_requests(self, work, stale):
        """Bring a published candidate up to date with the default branch.

        A PR GitHub reports as DIRTY (or BEHIND where up-to-date branches are
        required) never gets checks. Merge the default branch into the
        candidate: a conflict raises ``git.conflict`` (routed to a conflict
        revision by ``tick``); a clean merge restarts validation and the
        republish pushes the new head to the PR branch. Bounded per item.
        """
        work_id = work["id"]
        data = work["data"]
        states = {pr["repository"]: pr.get("merge_state") for pr in stale}
        used = data.get("pr_resyncs", 0)
        if used >= self.config.github_max_resyncs:
            self._defer(
                work,
                f"Pull request still {states} after {used} default-branch "
                "syncs; manual resolution needed",
            )
            return
        self.github.adapter.refresh_defaults(data["entries"])
        try:
            entries, changed = self.git.sync(data["entries"], strict=True)
        except GitFailure as error:
            if error.code == "git.conflict":
                data["pr_resyncs"] = used + 1
                self.state.update_work(
                    work_id,
                    data=data,
                    event="pull_requests.resync_conflict",
                )
            raise
        if not changed:
            # The candidate already contains the default branch; GitHub's
            # mergeability is stale or needs a human. Keep polling.
            self.state.update_work(
                work_id,
                data=data,
                control="backoff",
                wake_at=time.time() + self.config.github_poll_seconds,
                event="pull_requests.resync_noop",
            )
            return
        data["entries"] = entries
        data["pr_resyncs"] = used + 1
        data.pop("checks_candidate", None)
        data.pop("qa_candidate", None)
        self.state.update_work(
            work_id,
            phase="checking",
            control="ready",
            wake_at=0,
            data=data,
            event="pull_requests.resynced",
        )

    def _synchronize(self, work_id, data, unchanged_phase):
        """Merge an advanced default branch into the candidate.

        Runs before checks and QA so validation fixes landed on the default
        branch apply to the candidate. A changed candidate restarts at
        checking; otherwise the work moves to ``unchanged_phase`` (if any).
        """
        if self.github:
            self.github.adapter.refresh_defaults(data["entries"])
        data["entries"], changed = self.git.sync(data["entries"])
        if changed or unchanged_phase:
            self.state.update_work(
                work_id,
                data=data,
                phase="checking" if changed else unchanged_phase,
                control="ready",
                event="candidate.synchronized",
            )
        return changed

    def _reconcile_worktree(self, work):
        """Commit partial edits an interrupted run left in the worktree.

        Retrying cannot change a dirty candidate, so the factory records the
        edits as an increment before the next implementer starts. Returns the
        repository names that were snapshotted.
        """
        data = work["data"]
        pending = self.git.unrecorded(data["entries"])
        if not pending:
            return []
        try:
            self._refresh_candidate(work)
        except GitFailure as error:
            if error.code != "git.conflict":
                raise
            # Unresolved merge: the next implementer resolves it.
            return []
        self.state.update_work(
            work["id"], data=data, event="candidate.reconciled"
        )
        return pending

    def _conflict_revision(self, work, error):
        """Route a default-branch merge conflict in any phase to a revision.

        Finished work must not be deferred because the default branch moved:
        the merge is already prepared in the worktree, so the implementer
        resolves it and the candidate is re-validated. Accepted evidence and
        reports stay; the revision is flagged so reviewers focus on the merge.
        Conflict revisions do not consume the revision budget.
        """
        if (
            not isinstance(error, GitFailure)
            or error.code != "git.conflict"
            or not error.paths
            or work["phase"] in {"queued", "implementing"}
            or work["current_run"]
        ):
            return False
        try:
            fresh = self.state.get_work(work["id"])
            data = fresh["data"]
            resolver = "implementer"
            if self.config.merge_manager_enabled:
                key = merge_attempt_key(data)
                attempts = data.setdefault("merge_attempts", {})
                if attempts.get(key, 0) >= (
                    self.config.merge_manager.max_merge_attempts
                ):
                    self._merge_exhausted(fresh, error, attempts[key])
                    return True
                attempts[key] = attempts.get(key, 0) + 1
                resolver = "merge_manager"
            data["revision_reason"] = (
                "Resolve merge conflicts with the new default branch: "
                + ", ".join(error.paths)
            )
            data["conflict_revision"] = {
                "paths": error.paths,
                "summary": error.summary,
                "from_phase": work["phase"],
                "resolver": resolver,
            }
            data.pop("checks_candidate", None)
            data.pop("qa_candidate", None)
            # The resolved candidate is new: earlier acceptance and
            # publication describe the old one and must be earned again.
            for key in (
                "acceptance_decision",
                "acceptance_candidate",
                "acceptance_config_digest",
            ):
                data.pop(key, None)
            if not any(
                pr.get("state") == "merged"
                for pr in data.get("pull_requests", [])
            ):
                data.pop("publication_acceptance", None)
            self.state.update_work(
                work["id"],
                phase="implementing",
                control="ready",
                current_run=None,
                data=data,
                event="work.conflict_revision_requested",
            )
        except StateConflict:
            self.state.event("operation.superseded", work["id"], {})
        return True

    def _merge_exhausted(self, work, error, attempts):
        """Merge-manager budget spent for this PR head: owner decides."""
        reason = (
            f"Merge manager made {attempts} attempt(s) at this PR head and "
            "the branch still conflicts with the default branch in: "
            + ", ".join(error.paths or [])
        )
        for pr in work["data"].get("pull_requests", []):
            row = get_pr(self.state, pr.get("slug", ""), pr.get("number", 0))
            if row is not None:
                set_pr(self.state, row, state="needs_owner", reason=reason)
        self.state.event(
            "merge_manager.exhausted",
            work["id"],
            {"attempts": attempts, "paths": error.paths},
        )
        self._defer(work, reason)

    def _synchronize_for_dispatch(self, work):
        """Merge the latest default branch before an implementer starts.

        Resumed or revised candidates may predate fixes on the default
        branch. A conflict leaves the merge prepared in the worktree for the
        implementer to resolve; returns packet notes describing what changed.
        """
        work_id = work["id"]
        data = work["data"]
        before = {entry["name"]: entry["base"] for entry in data["entries"]}
        salvaged = self._reconcile_worktree(work)
        if self.github:
            self.github.adapter.refresh_defaults(data["entries"])
        conflict = None
        try:
            data["entries"], changed = self.git.sync(data["entries"])
        except GitFailure as error:
            if error.code != "git.conflict":
                raise
            conflict, changed = error.summary, False
        if changed:
            self.state.update_work(
                work_id, data=data, event="candidate.synchronized"
            )
        merged = [
            entry["name"]
            for entry in data["entries"]
            if entry["base"] != before[entry["name"]]
        ]
        if not (merged or conflict or salvaged):
            return None
        notes = {}
        if salvaged:
            notes["previous_run_interrupted"] = {
                "committed_partial_work": salvaged,
                "guidance": (
                    "The previous implementer run was interrupted or failed "
                    "and left edits in the worktree. The factory committed "
                    "them as an increment in the listed repositories. They "
                    "may be incomplete or broken: review the committed "
                    "diff, continue from it, and do not start over."
                ),
            }
        if merged or conflict:
            notes["default_branch_merged"] = merged
            notes["guidance"] = (
                "The latest default branch was merged into your candidate "
                "worktree since earlier work or runs. Repository docs "
                "(AGENTS.md, README, contributing guides) and code may have "
                "changed: re-read them before continuing, and re-check "
                "earlier conclusions, since problems may already be fixed."
            )
        if conflict:
            notes["merge_conflict"] = conflict
        return notes

    def _dispatch(self, work, role, notes=None):
        work_id = work["id"]
        run_id = identity("run")
        directory = self.home / "assignments" / run_id
        directory.mkdir(parents=True)
        bundle = resolved_bundle(self.home, self.config)
        revision = digest(bundle)
        atomic_json(self.home / "revisions" / f"{revision}.json", bundle)
        roots = [
            {
                "name": entry["name"],
                "path": entry["worktree"],
                "access": entry["access"],
            }
            for entry in work["data"]["entries"]
        ]
        primary = next(
            root["path"] for root in roots if root["access"] == "write"
        )
        assignment = {
            "id": run_id,
            "work_id": work_id,
            "role": role,
            "home": str(self.home),
            "directory": primary,
            "roots": roots,
            "packet_path": str(directory / "task.md"),
            "role_path": str(directory / "role.md"),
            "schema_path": str(directory / "report-schema.json"),
            "report_path": str(directory / "report.json"),
            "started_path": str(directory / "started.json"),
            "status_path": str(directory / "status.json"),
            "assignment_path": str(directory / "assignment.json"),
            "session_dir": str(self.home / "sessions" / work_id / role),
            "config": self.config.model_dump(),
            "config_digest": revision,
            "fixture_scenario": work["payload"].get("fixture_scenario"),
            "trace": self.tracer.assignment_settings(directory),
        }
        Path(assignment["session_dir"]).mkdir(parents=True, exist_ok=True)
        Path(assignment["role_path"]).write_text(
            role_prompt(self.home, bundle, role)
        )
        atomic_json(
            Path(assignment["schema_path"]), WorkerReport.model_json_schema()
        )
        evidence = self.state.db.execute(
            "SELECT data FROM evidence WHERE work_id=? ORDER BY created DESC LIMIT 12",
            (work_id,),
        ).fetchall()
        packet = {
            "assignment": run_id,
            "work_item": work["payload"],
            "role": role,
            "repositories": work["data"]["entries"],
            "validation_plan": [
                {
                    "repository": entry["name"],
                    "working_directory": entry["worktree"],
                    "candidate_revision": entry["candidate"],
                    "checks": self.git.repositories[entry["name"]].checks,
                    "qa": self.git.repositories[entry["name"]].qa,
                    "flag_environments": self.git.repositories[
                        entry["name"]
                    ].flag_environments,
                    "timeout_seconds": self.config.command_timeout_seconds,
                }
                for entry in work["data"]["entries"]
            ],
            "validation_guidance": (
                "The validation plan declares executable argument arrays, "
                "working directories, and flag environments. Use these "
                "configured commands instead of guessing replacement tools. "
                "Report unavailable prerequisites and actual command results. "
                "The coordinator independently validates frozen candidates."
            ),
            "execution_guidance": self._execution_guidance(),
            "default_branch_sync": notes,
            "human_guidance": self._guidance(work_id),
            "prior_reports": work["data"].get("reports", {}),
            "pull_requests": work["data"].get("pull_requests", []),
            "revision_reason": work["data"].get("revision_reason"),
            "pr_repair": self._repair_packet(work, role),
            "conflict_revision": work["data"].get("conflict_revision"),
            **self._conflict_packet(work, role),
            "evidence": bound_evidence(
                [json.loads(row["data"]) for row in evidence]
            ),
            "qa_guidance": bundle["qa_guidance"],
            "report_contract": WorkerReport.model_json_schema(),
        }
        Path(assignment["packet_path"]).write_text(
            "# Factory task packet\n\n" + json.dumps(packet, indent=2) + "\n"
        )
        run = self.state.create_run(work_id, role, assignment)
        atomic_json(Path(assignment["assignment_path"]), run["assignment"])
        self.tracer.provenance(work, run)
        self._launch(run)

    def _conflict_packet(self, work, role):
        """Merge-manager context, or the reviewer's resolution checklist."""
        conflict = work["data"].get("conflict_revision")
        if not conflict:
            return {}
        if role == "reviewer":
            if conflict.get("resolver") != "merge_manager":
                return {}
            return {"conflict_resolution_checklist": CONFLICT_REVIEW_CHECKLIST}
        if role != "merge_manager":
            return {}
        return {
            "merge_context": {
                "rules": MERGE_MANAGER_RULES,
                "this_item": {
                    "title": work["payload"]["title"],
                    "instruction": work["payload"]["instruction"],
                    "acceptance": work["payload"]["acceptance"],
                },
                "conflicts": self.git.conflict_context(work["data"]["entries"]),
                "attempt": merge_attempt_number(work["data"]),
                "max_attempts": self.config.merge_manager.max_merge_attempts,
            }
        }

    def _repair_packet(self, work, role="implementer"):
        target = self._repair_target(work)
        row = get_pr(self.state, *target) if target else None
        if row is None:
            return None
        return {
            "pull_request": f"{row['slug']}#{row['number']}",
            "branch": row["branch"],
            "head": row["head"],
            "failing_checks": row["failing"],
            "log_excerpt": row["log_excerpt"],
            "rules": REPAIR_RULES,
            **(
                {"review_checklist": REVIEW_CHECKLIST}
                if role == "reviewer"
                else {}
            ),
            "guidance": (
                "Your worktree is the PR branch at its failing head. Fix "
                "it in place; the factory commits, validates and pushes to "
                "the same branch. Do not rebase or merge the default branch."
            ),
        }

    def _guidance(self, work_id):
        entries = self.state.guidance(work_id)
        if not entries:
            return []
        return [
            {
                "authority": GUIDANCE_AUTHORITY,
                "author": item["author"],
                "at": item["at"],
                "text": item["text"],
            }
            for item in entries
        ]

    def _execution_guidance(self):
        text = (
            "Leave changes uncommitted in the worktree: the factory commits "
            "and integrates them. Do not commit, push, or switch branches. "
            "If the worktree is mid-merge (MERGE_HEAD present, conflict "
            "markers in files), that is expected: edit the conflicted files "
            "to the correct combined content and remove every marker; never "
            "run git add, commit or merge --abort, the factory completes "
            "the merge after you report. Checks that need Docker, databases "
            "or other host services may not run where you do: call the "
            "factory_check tool, which runs the configured checks outside "
            "your environment on a snapshot of your worktree and returns "
            "pass/fail output; cite its result as your verification."
        )
        if self.config.isolation == "required":
            text += (
                " You run in a nono sandbox: only your worktrees, TMPDIR and "
                "agent config are writable; git objects, the rest of your "
                "home directory and host sockets are unavailable. Report a "
                "needed but unavailable path as a prerequisite, not a failure."
            )
        return text

    def _launch(self, run):
        with self.state.dispatch_lock():
            self._launch_under_lock(run)

    def _launch_under_lock(self, run):
        assignment = run["assignment"]
        work = self.state.get_work(run["work_id"])
        if self.state.is_paused() or work["control"] == "paused":
            raise StateConflict("dispatch was paused")
        operation = self.state.begin_operation(
            run["work_id"],
            "worker.launch",
            assignment,
            f"launch:{run['id']}",
        )
        existing = self.harness.find_existing(assignment)
        work = self.state.get_work(run["work_id"])
        if self.state.is_paused() or work["control"] == "paused":
            raise StateConflict("dispatch was paused during reconciliation")
        if existing:
            session = existing
        else:
            work = self.state.get_work(run["work_id"])
            previous = work["data"].get("sessions", {}).get(run["role"])
            reassign = getattr(self.harness, "reassign", None)
            if previous and reassign:
                try:
                    session = reassign(previous, assignment)
                except HarnessError as error:
                    if error.failure.get("uncertain"):
                        raise
                    session = self.harness.launch(assignment)
            else:
                session = self.harness.launch(assignment)
        self.state.update_run(run["id"], state="running", session=session)
        self.state.finish_operation(operation, session)
        self.state.event(
            "worker.dispatched",
            run["work_id"],
            {
                "run_id": run["id"],
                "role": run["role"],
                "session": session,
            },
        )
        work = self.state.get_work(run["work_id"])
        if self.state.is_paused() or work["control"] == "paused":
            observation = self.harness.interrupt(session)
            self.state.event(
                "dispatch.paused_after_launch", work["id"], observation
            )

    def _service_checks(self, work, run):
        """Run worker-requested checks here, outside the worker's sandbox."""
        from .claude_worker import answer_check, pending_checks, summarize_check

        assignment = run["assignment"]
        for request_id in pending_checks(assignment):
            try:
                rows = self.git.check_worktree(work["data"]["entries"])
                digest_now = digest(resolved_bundle(self.home, self.config))
                for row in rows:
                    row["config_digest"] = digest_now
                    self.state.evidence(work["id"], row, run["id"])
                result = summarize_check(rows)
                if not rows:
                    result["error"] = "No check commands are configured"
            except (GitFailure, OSError, ValueError) as error:
                result = {"passed": False, "error": str(error)[:2000]}
            answer_check(assignment, request_id, result)

    def _collect(self, work):
        run = self.state.get_run(work["current_run"])
        assignment = run["assignment"]
        if run["session"] is None:
            self._launch(run)
            return
        self._service_checks(work, run)
        observation = self.harness.observe(run["session"], assignment)
        if (observation.get("failure") or {}).get("code") in {
            "harness.error",
            "harness.exited",
        }:
            limited = self._worker_limit(run)
            if limited:
                observation["failure"] = limited.model_dump()
        self.tracer.ingest(run)
        if observation.get("failure") or observation["state"] in {
            "settled",
            "finished",
        }:
            self.tracer.finish_run(run, work)
        self.state.observe(work["id"], {"run_id": run["id"], **observation})
        report_path = Path(assignment["report_path"])
        if report_path.exists() and run["report"] is None:
            try:
                envelope = json.loads(report_path.read_text())
                self.state.accept_report(run["id"], envelope)
                run = self.state.get_run(run["id"])
            except (ValueError, KeyError, ValidationError) as error:
                self._failure(
                    work["id"],
                    Failure(
                        code="report.invalid",
                        summary=str(error)[:2000],
                        source="adapter",
                    ),
                )
                return
        if observation.get("failure"):
            self._failure(
                work["id"], Failure.model_validate(observation["failure"])
            )
            return
        if observation["state"] in {"settled", "finished"}:
            if run["report"] is not None:
                self._report(work, run)
            elif (
                time.time() - run["created"] > 3
                or self.config.harness == "fixture"
            ):
                self._failure(
                    work["id"],
                    self._worker_limit(run)
                    or Failure(
                        code="report.missing",
                        summary="Worker settled without a valid report",
                        source="runtime",
                    ),
                )
        elif time.time() - run["created"] > self._phase_timeout(work):
            self._failure(
                work["id"],
                Failure(
                    code="phase.timeout",
                    summary="Assignment exceeded its phase deadline",
                    source="coordinator",
                ),
            )

    def _worker_limit(self, run):
        """A provider limit recorded at the end of the worker transcript."""
        try:
            started = json.loads(
                Path(run["assignment"]["started_path"]).read_text()
            )
        except (OSError, ValueError):
            return None
        return transcript_failure(started.get("session_file"))

    def _phase_timeout(self, work):
        run = self.state.get_run(work["current_run"])
        if run["role"] in WRITER_ROLES:
            return (
                self.config.implementer_phase_timeout_seconds
                or self.config.phase_timeout_seconds
            )
        return self.config.phase_timeout_seconds

    def _decide(
        self,
        work,
        allowed,
        preferred,
        report=None,
        evaluation=None,
        contract_error=None,
    ):
        bundle = resolved_bundle(self.home, self.config)
        snapshot = {
            "work": work["payload"],
            "phase": work["phase"],
            "report": report,
            "preferred": preferred,
            "recoveries": work["recoveries"],
            "revisions": work["revisions"],
            "candidate": work["data"].get("entries"),
            "conflict_revision": work["data"].get("conflict_revision"),
            "evaluation": evaluation,
            "human_guidance": self._guidance(work["id"]),
        }
        change = work["data"].get("issue_change")
        if change and not change.get("surfaced"):
            snapshot["issue_change"] = {
                "note": (
                    "The source issue was edited after acceptance; accepted "
                    "scope above is unchanged. Judge whether the edit "
                    "(e.g. a moved criterion) matters; this is shown once."
                ),
                "changed": change["changed"],
                "observed": change["observed"],
            }
            data = {
                **work["data"],
                "issue_change": {**change, "surfaced": True},
            }
            try:
                self.state.update_work(
                    work["id"], data=data, event="intake.issue_surfaced"
                )
            except StateConflict:
                pass
        snapshot["verification_policy"] = (
            "Workers run in a sandbox that cannot reach Docker, databases or "
            "other host services, so missing worker-side verification of "
            "that kind is not a reason to defer: advance to checking "
            "(the factory runs the configured checks outside the sandbox) "
            "or revise with a concrete correction. Defer only for a "
            "genuine human decision. If human_guidance is present it is "
            "authoritative: it overrides earlier conclusions and answers "
            "the question it addresses, so never defer again for that "
            "question; defer only for a new, different decision."
        )
        if evaluation in {"acceptance", "handoff"}:
            snapshot["evidence"] = self._current_evidence(work)
            snapshot["citable_evidence_ids"] = sorted(
                citable_ids(snapshot["evidence"])
            )
            snapshot["evidence_citation_rules"] = (
                "evidence_refs may contain ONLY ids listed in "
                "citable_evidence_ids. Rows with citable=false (worker_check "
                "snapshots) and ids from earlier reports or candidates are "
                "rejected. A 'passed' criterion needs at least one ref."
            )
        if contract_error:
            snapshot["contract_error"] = contract_error
        if evaluation == "handoff":
            next_phase = {
                "implementing": "checking",
                "reviewing": "pushing"
                if self._repair_target(work)
                else "preparing",
                "qa": "integrating",
            }[work["phase"]]
            advance_meanings = {
                "checking": (
                    "Snapshot the implementation and execute independent "
                    "checks; this grants neither integration nor completion."
                ),
                "pushing": (
                    "Push the reviewed repair to the PR branch; CI then "
                    "judges it."
                ),
                "preparing": (
                    "Synchronize the reviewed candidate and run required QA; "
                    "this does not authorize a merge or completion."
                ),
                "integrating": (
                    "Evaluate independent integration and acceptance gates; "
                    "this handoff alone does not authorize a merge."
                ),
            }
            meanings = {
                "advance": advance_meanings[next_phase],
                "revise": (
                    "Return to implementation with the concrete correction "
                    "described in the decision rationale."
                ),
                "retry": (
                    "Request another implementation attempt within the "
                    "configured revision budget."
                ),
                "defer": (
                    "Park only this work item with the decision rationale; "
                    "other eligible work can continue."
                ),
            }
            snapshot["handoff"] = {
                "next_phase": next_phase,
                "action_meanings": {
                    action: meanings[action] for action in sorted(allowed)
                },
                "evidence_scope": (
                    "Current candidate set and configuration. Evidence from "
                    "later phases is not required for this handoff."
                ),
            }
        if evaluation == "acceptance":
            snapshot["acceptance_policy"] = {
                "stage": "post_merge"
                if work["phase"] == "awaiting_merge"
                else "pre_publication",
                "complete": "All product criteria pass with current evidence; publish and confirm merges before closing the issue.",
                "advance": "Publish a safe increment only when explicitly assessed product criteria remain incomplete or unverifiable.",
                "criterion_results": "passed (needs citable evidence_refs); incomplete; unverifiable (no configured check can prove it; needs no refs, blocks complete, allows advance); waived (satisfied by authoritative human_guidance; no refs); failed.",
                "worker_remaining_work": work["data"]
                .get("reports", {})
                .get("implementer", {})
                .get("remaining_work", []),
                "boundary": "Publication, merging, issue closure and coordinator handoffs are factory duties, not unfinished product scope. Worker remaining-work text is advisory; assess every criterion.",
            }
        guard = (
            nullcontext()
            if self.config.concurrency.coordinator_parallel_decisions
            else self._decision_lock
        )
        with guard:
            decision = self.coordinator.decide(
                snapshot, allowed, bundle["prompts"]["orchestrator"]
            )
        current = self.state.get_work(work["id"])
        if (
            current["updated"] != work["updated"]
            or current["control"] == "paused"
            or self.state.is_paused()
        ):
            raise StateConflict(
                "work changed while the coordinator was deciding"
            )
        self.state.event(
            "coordinator.decision",
            work["id"],
            {
                "decision": decision.model_dump(),
                "allowed": sorted(allowed),
                "state_version": work["updated"],
                "config_digest": digest(bundle),
                "model": self.config.coordinator_model,
                "strategy": self.config.coordinator,
            },
        )
        return (
            decision
            if evaluation in {"acceptance", "handoff"}
            else decision.action
        )

    def _acceptance_intent(self, work, decision):
        intent, remaining = acceptance_intent(
            decision,
            len(work["payload"]["acceptance"]),
            citable_ids(self._current_evidence(work)),
        )
        if any(a.result == "waived" for a in decision.acceptance) and not (
            self._guidance(work["id"])
        ):
            raise AcceptanceContractError(
                ["'waived' requires authoritative human_guidance, none exists"],
                citable_ids(self._current_evidence(work)),
            )
        return intent, remaining

    def _current_evidence(self, work):
        return current_evidence(self.state, self.home, self.config, work)

    def _report(self, work, run):
        report = WorkerReport.model_validate(run["report"])
        if report.outcome == "error":
            self._failure(
                work["id"],
                Failure(
                    code=report.error_code or "worker.unknown",
                    summary=report.summary,
                    source="worker",
                    retry_after_seconds=report.retry_after_seconds,
                ),
            )
            return
        if run["role"] in {"reviewer", "qa"} and report.outcome == "ready":
            required = {entry["name"] for entry in work["data"]["entries"]}
            if not required.issubset(set(report.reviewed_repositories)):
                raise ValueError("worker report omitted assigned repositories")
        required_findings = any(
            finding.severity == "required" for finding in report.findings
        )
        if report.outcome == "defer":
            allowed, preferred = {"defer", "retry"}, "defer"
        elif report.outcome == "revise" or required_findings:
            allowed, preferred = {"revise", "defer"}, "revise"
        else:
            allowed, preferred = {"advance", "revise", "defer"}, "advance"
        if (
            run["role"] in WRITER_ROLES
            and report.outcome == "ready"
            and not required_findings
        ):
            current = self.state.get_work(work["id"])
            if (
                current["updated"] != work["updated"]
                or current["control"] == "paused"
                or self.state.is_paused()
            ):
                raise StateConflict(
                    "work changed before implementation handoff"
                )
            action = "advance"
            rationale = (
                "Implementation is ready to snapshot and independently check; "
                "acceptance and integration remain separately gated."
            )
            self.state.event(
                "handoff.authorized",
                work["id"],
                {
                    "run_id": run["id"],
                    "next_phase": "checking",
                    "strategy": "deterministic",
                    "rationale": rationale,
                },
            )
        else:
            if run["role"] in WRITER_ROLES:
                # The coordinator must judge what the implementer actually
                # committed, not the candidate frozen at prepare time.
                self._refresh_candidate(work)
            decision = self._decide(
                work,
                allowed,
                preferred,
                report.model_dump(),
                evaluation="handoff",
            )
            action = decision.action
            rationale = decision.rationale
        for improvement in report.improvements:
            propose(self.state, self.config, work["id"], improvement)
        data = work["data"]
        data.setdefault("reports", {})[run["role"]] = report.model_dump()
        data.setdefault("report_config_digests", {})[run["role"]] = run[
            "assignment"
        ]["config_digest"]
        keep = self.config.keep_finished_sessions
        sessions = data.setdefault("sessions", {})
        if keep:
            sessions[run["role"]] = run["session"]
        else:
            # A closed session cannot be rebound; the next run starts fresh.
            sessions.pop(run["role"], None)
        if run["role"] in {"reviewer", "qa"} and action == "advance":
            self.state.evidence(
                work["id"],
                {
                    "kind": run["role"],
                    "passed": True,
                    "report": report.model_dump(),
                    "candidate_set": {
                        entry["name"]: entry["candidate"]
                        for entry in data["entries"]
                    },
                    "config_digest": run["assignment"]["config_digest"],
                },
                run["id"],
            )
        self.state.update_run(run["id"], state="settled")
        work = self.state.update_work(work["id"], current_run=None, data=data)
        if not keep:
            self._close_session(work["id"], run, "completed")
        repair = self._repair_target(work)
        if action in {"revise", "retry"}:
            if repair and run["role"] == "reviewer":
                self._repair_rejected(work, repair, rationale)
            else:
                self._revise(work, rationale)
        elif action == "defer":
            self._defer(work, rationale)
        else:
            phase = {
                "implementer": "checking",
                "merge_manager": "checking",
                "reviewer": "pushing" if repair else "preparing",
                "qa": "integrating",
            }[run["role"]]
            self.state.update_work(
                work["id"],
                phase=phase,
                control="ready",
                wake_at=0,
                event="work.advanced",
            )

    def _refresh_candidate(self, work):
        """Snapshot worktree HEAD into the work entries held in memory."""
        data = work["data"]
        operation = self.state.begin_operation(work["id"], "git.snapshot", {})
        data["entries"] = self.git.snapshot(
            data["entries"], data.get("workspace_id", work["id"])
        )
        self.state.finish_operation(operation, {"entries": data["entries"]})

    def _validate(self, work_id, kind, next_phase):
        work = self.state.get_work(work_id)
        evidence = self.git.validate(work["data"]["entries"], kind)
        for result in evidence:
            result["config_digest"] = digest(
                resolved_bundle(self.home, self.config)
            )
            self.state.evidence(work_id, result)
        if not evidence or any(not result["passed"] for result in evidence):
            self._revise(
                work, f"Required {kind} failed; inspect recorded evidence"
            )
            return
        data = work["data"]
        data[f"{kind}_candidate"] = {
            entry["name"]: entry["candidate"] for entry in data["entries"]
        }
        data[f"{kind}_config_digest"] = digest(
            resolved_bundle(self.home, self.config)
        )
        self.state.update_work(
            work_id,
            phase=next_phase,
            control="ready",
            data=data,
            event=f"validation.{kind}.passed",
        )

    def _integrate(self, work):
        work_id = work["id"]
        data = work["data"]
        if self.github:
            self.github.adapter.refresh_defaults(data["entries"])
        current_digest = digest(resolved_bundle(self.home, self.config))
        validation_current = all(
            data.get(f"{kind}_config_digest") == current_digest
            for kind in ("checks", "qa")
        )
        reports_current = all(
            data.get("report_config_digests", {}).get(role) == current_digest
            for role in ("reviewer", "qa")
        )
        if not validation_current or not reports_current:
            self.state.update_work(
                work_id,
                phase="checking",
                control="ready",
                event="validation.configuration_changed",
            )
            return
        # Strict: an unresolved merge left by a deferral or resume must route
        # to the conflict revision, never publish the stale candidate.
        entries, changed = self.git.sync(data["entries"], strict=True)
        if changed:
            data["entries"] = entries
            self.state.update_work(work_id, phase="checking", data=data)
            return
        candidates = {entry["name"]: entry["candidate"] for entry in entries}
        if any(
            data.get(f"{kind}_candidate") != candidates
            for kind in ("checks", "qa")
        ):
            raise ValueError("candidate changed after required validation")
        reports = data.get("reports", {})
        if not all(
            reports.get(role, {}).get("outcome") == "ready"
            for role in ("reviewer", "qa")
        ):
            raise ValueError("integration lacks required review and QA reports")
        compatibility = self.git.validate_integration_plan(entries)
        for result in compatibility:
            result["config_digest"] = current_digest
            self.state.evidence(work_id, result)
        if any(not result["passed"] for result in compatibility):
            self._revise(work, "Intermediate repository compatibility failed")
            return
        remaining = reports.get("implementer", {}).get("remaining_work", [])
        allowed = {"advance", "complete", "revise", "defer"}
        preferred = "advance" if remaining else "complete"
        contract_error = None
        for attempt in range(CONTRACT_RETRIES + 1):
            decision = self._decide(
                work,
                allowed,
                preferred,
                evaluation="acceptance",
                contract_error=contract_error,
            )
            if decision.action == "revise":
                self._revise(work, decision.rationale)
                return
            if decision.action == "defer":
                self._defer(work, decision.rationale)
                return
            if (
                digest(resolved_bundle(self.home, self.config))
                != current_digest
            ):
                self.state.update_work(
                    work_id,
                    phase="checking",
                    control="ready",
                    event="validation.configuration_changed",
                )
                return
            try:
                intent, remaining = self._acceptance_intent(work, decision)
                break
            except AcceptanceContractError as error:
                # A reasonable decision with a fixable citation mistake must
                # not park the item: tell the coordinator exactly what failed.
                self.state.event(
                    "coordinator.decision_rejected",
                    work_id,
                    {
                        "attempt": attempt + 1,
                        "retries_allowed": CONTRACT_RETRIES,
                        "problems": error.problems,
                        "citable_evidence_ids": error.citable,
                        "will_retry": attempt < CONTRACT_RETRIES,
                    },
                )
                if attempt == CONTRACT_RETRIES:
                    raise ValueError(
                        f"{error} (after {attempt + 1} attempts)"
                    ) from error
                contract_error = {
                    "message": "Your previous decision was rejected. Fix it "
                    "and decide again.",
                    "problems": error.problems,
                    "citable_evidence_ids": error.citable,
                    "previous_decision": decision.model_dump(),
                }
        if intent.action != decision.action:
            self.state.event(
                "acceptance.intent_normalized",
                work_id,
                {
                    "original_action": decision.action,
                    "action": intent.action,
                    "reason": "All product acceptance criteria passed",
                },
            )
        data["acceptance_decision"] = intent.model_dump()
        data["acceptance_candidate"] = candidates
        data["acceptance_config_digest"] = current_digest
        work = self.state.update_work(
            work_id, data=data, event="acceptance.intent_recorded"
        )
        if self.github:
            self.github.publish(work, entries, remaining)
            return
        operation = self.state.begin_operation(
            work_id,
            "git.integrate",
            {"candidates": candidates},
            f"integrate:{work_id}:{digest(candidates)}",
        )
        if (
            self.state.is_paused()
            or self.state.get_work(work_id)["control"] == "paused"
        ):
            raise StateConflict("integration was paused")
        result = self.git.integrate(entries)
        self.state.finish_operation(operation, {"repositories": result})
        data["integration"] = result
        if remaining:
            data.pop("conflict_revision", None)
            data["revision_reason"] = "Continue remaining scope: " + "; ".join(
                remaining
            )
            data["increment"] = data.get("increment", 0) + 1
            for key in ("entries", "reports"):
                data.pop(key, None)
            self.state.update_work(
                work_id,
                phase="queued",
                control="ready",
                data=data,
                event="increment.integrated",
            )
        else:
            self.state.update_work(
                work_id,
                phase="completed",
                control="settled",
                data=data,
                event="work.completed",
                notice={
                    "kind": "completed",
                    "message": f"{work['title']} completed in managed clones",
                    "incident_key": f"completed:{work_id}",
                },
            )

    def _repair_rejected(self, work, target, reason):
        """Review rejection spends repair budget; exhaustion escalates."""
        if self.tracker.reject_repair(*target, reason):
            self._revise(work, f"Review rejected the repair: {reason}")
            return
        self.state.update_work(
            work["id"],
            phase="completed",
            control="settled",
            event="work.repair_abandoned",
        )

    def _revise(self, work, reason):
        if work["revisions"] >= self.config.max_revisions:
            self._defer(work, "Revision budget exhausted: " + reason)
            return
        data = work["data"]
        data["revision_reason"] = reason
        data.pop("conflict_revision", None)
        data.pop("checks_candidate", None)
        data.pop("qa_candidate", None)
        self.state.update_work(
            work["id"],
            phase="implementing",
            control="ready",
            current_run=None,
            revisions=work["revisions"] + 1,
            data=data,
            event="work.revision_requested",
        )

    def _defer(self, work, reason):
        self.state.update_work(
            work["id"],
            control="deferred",
            event="work.deferred",
            notice={
                "kind": "action_required",
                "message": reason,
                # Deferrals without a new run share a generation; key on the
                # reason too so a different reason is never dropped.
                "incident_key": (
                    f"deferred:{work['id']}:{work['generation']}:"
                    f"{digest({'reason': reason})[:16]}"
                ),
            },
        )
        self._ask_owner(work, reason)

    def _failure(self, work_id, failure):
        work = self.state.get_work(work_id)
        if work["control"] == "paused" or self.state.is_paused():
            self.state.event(
                "failure.during_pause", work_id, failure.model_dump()
            )
            return
        self.state.event("failure.observed", work_id, failure.model_dump())
        recovery = plan_recovery(
            failure,
            work["recoveries"],
            self.config.max_recoveries,
            self.config.retry_base_seconds,
        )
        known_codes = {
            "auth.required",
            "permission.required",
            "config.invalid",
            "validation.unconfigured",
            "scope.denied",
            "quota.exhausted",
            "storage.unavailable",
            "provider.rate_limited",
            "provider.unavailable",
            "harness.exited",
            "harness.error",
            "report.invalid",
            "report.missing",
            "phase.timeout",
            "coordinator.failed",
            "github.no_changes",
            "git.candidate_changed",
        }
        if (
            failure.code not in known_codes
            and work["phase"] != "awaiting_merge"
            and not failure.uncertain
            and work["recoveries"] < self.config.max_recoveries
        ):
            try:
                action = self._decide(
                    work,
                    {"retry", "revise", "defer"},
                    "retry",
                    report={
                        "failure": failure.model_dump(),
                        "recent_evidence": self.state.inspect()["evidence"][
                            -6:
                        ],
                    },
                    evaluation="recovery",
                )
                recovery = type(recovery)(
                    action, recovery.delay, recovery.notify
                )
                if action == "revise" and not work["current_run"]:
                    self._revise(work, failure.summary)
                    return
            except CoordinatorError:
                self.state.event("recovery.diagnosis_unavailable", work_id, {})
            except StateConflict:
                self.state.event("recovery.superseded", work_id, {})
                return
        if recovery.action == "pause_factory":
            self.state.pause()
            return
        run_id = work["current_run"]
        exhausted = work["recoveries"] >= self.config.max_recoveries
        if failure.uncertain and exhausted:
            recovery = type(recovery)("defer", 0, "action_required")
        if run_id and (not failure.uncertain or exhausted):
            run = self.state.get_run(run_id)
            ends = failure.source != "coordinator" or run["report"] is None
            interrupted = False
            try:
                if run["session"]:
                    observation = self.harness.observe(
                        run["session"], run["assignment"]
                    )
                    if observation["state"] not in {"settled", "finished"}:
                        result = self.harness.interrupt(run["session"])
                        interrupted = True
                        self.state.event("worker.interrupted", work_id, result)
                        observation = self.harness.observe(
                            run["session"], run["assignment"]
                        )
                        # An interrupted worker is closed whether or not it
                        # then reports settled; it must not idle at a prompt.
                        self.tracer.finish_run(run, work)
                        try:
                            self.harness.close(run["session"])
                        except HarnessError as error:
                            if observation["state"] not in {
                                "settled",
                                "finished",
                            }:
                                raise
                            # Quiesced but still open: the reaper retries.
                            self.state.event(
                                "worker.session_close_failed",
                                work_id,
                                {
                                    "run_id": run_id,
                                    "reason": "interrupted",
                                    **error.failure,
                                },
                            )
                        else:
                            self.state.event(
                                "worker.session_closed",
                                work_id,
                                {"run_id": run_id, "reason": "interrupted"},
                            )
            except HarnessError as error:
                self.state.update_work(
                    work_id,
                    control="quarantined",
                    event="worker.termination_unconfirmed",
                    notice={
                        "kind": "action_required",
                        "message": "Worker termination is unconfirmed; preserved worktree",
                        "incident_key": f"quarantine:{run_id}",
                    },
                )
                self._ask_owner(
                    work, "Worker termination is unconfirmed; worktree kept"
                )
                self.state.event(
                    "recovery.action_failed", work_id, error.failure
                )
                return
            if ends or interrupted:
                self.state.update_run(run_id, state="failed")
                run_id = None
                if not interrupted and not self.config.keep_finished_sessions:
                    self.tracer.finish_run(run, work)
                    self._close_session(work_id, run, "failed")
        if recovery.action == "defer":
            if run_id != work["current_run"]:
                work = self.state.update_work(work_id, current_run=run_id)
            self._defer(work, failure.summary)
            return
        if recovery.action == "revise":
            if run_id != work["current_run"]:
                work = self.state.update_work(work_id, current_run=run_id)
            self._revise(work, failure.summary)
            return
        if recovery.action == "rate_limit":
            until = self.state.set_rate_limit(time.time() + recovery.delay)
            reset = time.strftime("%H:%M", time.localtime(until))
            self.state.update_work(
                work_id,
                current_run=run_id,
                control="backoff",
                wake_at=until,
                event="recovery.rate_limited",
                notice={
                    "kind": recovery.notify,
                    "message": (
                        f"{failure.summary}; dispatch paused until {reset}, "
                        "then resumes automatically"
                    ),
                    "incident_key": f"ratelimit:{int(until)}",
                },
            )
            return
        self.state.update_work(
            work_id,
            current_run=run_id,
            control="backoff",
            wake_at=time.time() + recovery.delay,
            recoveries=work["recoveries"] + 1,
            event="recovery.scheduled",
            notice={
                "kind": recovery.notify,
                "message": f"{failure.summary}; autonomous {recovery.action} scheduled",
                "incident_key": f"recovery:{work_id}:{work['generation']}:{failure.code}",
            },
        )

    def _close_session(self, work_id, run, reason):
        """Best-effort close of an ended run; the reaper retries failures."""
        close = getattr(self.harness, "close", None)
        if not run.get("session") or close is None:
            return False
        try:
            close(run["session"])
        except HarnessError as error:
            self.state.event(
                "worker.session_close_failed",
                work_id,
                {"run_id": run["id"], "reason": reason, **error.failure},
            )
            return False
        self.state.event(
            "worker.session_closed",
            work_id,
            {"run_id": run["id"], "reason": reason},
        )
        return True

    def orphaned_sessions(self):
        """Owned worker terminals whose run is no longer a current run."""
        lister = getattr(self.harness, "owned_terminals", None)
        if lister is None:
            return []
        terminals = lister()
        # Runs are created (and made current) before their terminal exists,
        # so reading current runs after listing cannot miss a fresh launch.
        return orphaned_terminals(
            terminals, self.state.list_work(), self.config.name
        )

    def reap_sessions(self):
        """Close owned worker terminals left behind by ended runs."""
        self._reaped_at = time.monotonic()
        reaped = []
        with self.state.dispatch_lock():
            try:
                orphans = self.orphaned_sessions()
            except HarnessError as error:
                self.state.event("reaper.unavailable", "factory", error.failure)
                return reaped
            for terminal in orphans:
                labels = terminal["labels"]
                assignment = labels["assignment"]
                try:
                    run_state = self.state.get_run(assignment)["state"]
                except ValueError:
                    run_state = None
                if self.config.keep_finished_sessions and run_state in {
                    "settled",
                    "failed",
                }:
                    continue
                details = {
                    "address": terminal["address"],
                    "assignment": assignment,
                    "run_state": run_state,
                    "role": labels.get("role"),
                }
                subject = labels.get("work") or "factory"
                try:
                    self.harness.close_terminal(terminal)
                except HarnessError as error:
                    self.state.event(
                        "worker.session_reap_failed",
                        subject,
                        {**details, **error.failure},
                    )
                    continue
                self.state.event("worker.session_reaped", subject, details)
                reaped.append(details)
        return reaped

    def pause(self, work_id=None):
        with self.state.dispatch_lock():
            result = self.state.pause(work_id)
            work_items = self.state.list_work()
        handoffs = []
        for work in work_items:
            if work_id and work["id"] != work_id:
                continue
            if work["current_run"]:
                run = self.state.get_run(work["current_run"])
                if run["session"]:
                    try:
                        observation = self.harness.interrupt(run["session"])
                    except HarnessError as error:
                        observation = {"failure": error.failure}
                    self.state.event(
                        "human.takeover_requested", work["id"], observation
                    )
                    handoffs.append({"work_id": work["id"], **observation})
        result["handoffs"] = handoffs
        result["handoff_confirmed"] = all(
            item.get("outcome") in {"stopped", "exited"} for item in handoffs
        )
        return result

    def resume(self, work_id=None, guidance=None, author="human"):
        if guidance:
            if not work_id:
                raise ValueError("guidance requires --work")
            self.state.add_guidance(work_id, guidance, author)
        result = self.state.resume(work_id)
        if guidance:
            result["guidance"] = len(self.state.guidance(work_id))
        return result

    def flush_notifications(self):
        return flush(self.state, self.config)
