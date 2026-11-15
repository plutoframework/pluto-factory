"""Stateful pull-request tracking with bounded self-healing repair.

States: open -> pending -> failed -> repairing -> green -> ready -> merged,
plus needs_owner (a human must act), closed and untracked. A failed head SHA
earns at most ``pr_max_repairs`` repairs; non-code-fixable failures and
exhausted budgets escalate to the owner instead of looping.
"""

import re
import time

from .advisor import fallback_advice, terse
from .attention import (
    OPEN_PR_STATES,
    add_decision,
    fill_number,
    get_pr,
    list_prs,
    pending_intents,
    set_pr,
    track_pr,
)
from .github import GitHubAdapter, GitHubFailure
from .github_checks import merge_check_status
from .models import Submission, workable_repositories
from .state import StateConflict

EXCERPT_LIMIT = 3000
LOG_RUNS = 2
# A repository with no CI at all must not wait forever for checks.
NO_CHECKS_GRACE_SECONDS = 600
PR_FIELDS = (
    "number,title,url,state,headRefOid,headRefName,baseRefName,"
    "reviewDecision,statusCheckRollup,mergeStateStatus,mergedAt,isDraft"
)
ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
RUN_ID = re.compile(r"/actions/runs/(\d+)")
HUMAN_ONLY_CHECK = re.compile(r"codeql|code.?scanning", re.IGNORECASE)
HUMAN_ONLY_LOG = re.compile(
    r"resource not accessible by integration|bad credentials|"
    r"http 40[13]\b|permission denied \(publickey\)|"
    r"(?:secret|token|credential)s? .{0,40}(?:not (?:set|found|provided)|"
    r"missing|empty|is required)|"
    r"required (?:secret|variable) .{0,40}(?:missing|not set)",
    re.IGNORECASE,
)
REPAIR_RULES = (
    "Repair rules: fix the production cause of the failure. Do NOT weaken, "
    "delete or skip tests, loosen assertions, add lint/type suppressions, "
    "or edit CI configuration to make checks pass. If the failure cannot be "
    "fixed in code (secrets, permissions, scanning alerts, flaky "
    "infrastructure), report outcome=defer and explain."
)
REVIEW_CHECKLIST = (
    "Repair review checklist: REJECT (outcome=revise) if the diff deletes or "
    "skips tests, relaxes or removes assertions, adds lint/type/scanner "
    "suppressions, widens allowed values, edits CI configuration, or bumps "
    "timeouts/retries without a demonstrated cause. Approve only a change "
    "that fixes the production cause of the recorded failure."
)
FAILURE_RANK = {"FAILURE", "ERROR", "TIMED_OUT", "ACTION_REQUIRED"}


def repair_source(slug, number):
    return f"repair:{slug}#{number}"


def parse_repair_source(source):
    match = re.fullmatch(r"repair:([^#]+)#(\d+)", source or "")
    return (match[1], int(match[2])) if match else None


def classify(view, row, config, now=None):
    """Pure observation -> target state for one PR (no side effects).

    Returns a dict: state, reason, failing, fixable (failed only).
    """
    now = now or time.time()
    head = view.get("headRefOid") or ""
    if view.get("mergedAt") or str(view.get("state", "")).upper() == "MERGED":
        return {"state": "merged", "reason": "Merged", "failing": []}
    if str(view.get("state", "")).upper() == "CLOSED":
        return {"state": "closed", "reason": "Closed", "failing": []}
    checks = view.get("statusCheckRollup") or []
    status = merge_check_status(
        {
            "head": head,
            "observed_head": head,
            "head_changed": False,
            "checks": checks,
        },
        list(config.github_required_checks),
    )
    review = view.get("reviewDecision") or ""
    if review == "CHANGES_REQUESTED":
        return {
            "state": "needs_owner",
            "reason": "A reviewer requested changes",
            "failing": [],
        }
    if status.get("failed"):
        failing = status["failed"]
        human = [name for name in failing if HUMAN_ONLY_CHECK.search(name)]
        return {
            "state": "failed",
            "reason": "Failing checks: " + ", ".join(failing),
            "failing": failing,
            "fixable": not human,
            "why_not": (
                "code-scanning alert needs a human dismissal or decision"
                if human
                else ""
            ),
        }
    if status.get("pending") or status.get("incomplete"):
        names = status.get("pending", []) + status.get("incomplete", [])
        return {
            "state": "pending",
            "reason": "Checks running: " + ", ".join(sorted(names)),
            "failing": [],
        }
    if status.get("missing"):
        return {
            "state": "pending",
            "reason": "Required checks not reported: "
            + ", ".join(status["missing"]),
            "failing": [],
        }
    if not checks and now - row["state_since"] < NO_CHECKS_GRACE_SECONDS:
        return {
            "state": "pending",
            "reason": "No checks reported yet",
            "failing": [],
        }
    merge_state = view.get("mergeStateStatus") or "UNKNOWN"
    if review == "REVIEW_REQUIRED":
        return {
            "state": "needs_owner",
            "reason": "Checks passed; a required review is outstanding",
            "failing": [],
        }
    if view.get("isDraft"):
        return {
            "state": "green",
            "reason": "Checks passed; the PR is a draft",
            "failing": [],
        }
    if merge_state == "DIRTY":
        if row["origin"] == "factory":
            return {
                "state": "green",
                "reason": "Checks passed; resyncing with the base branch",
                "failing": [],
            }
        return {
            "state": "needs_owner",
            "reason": "Checks passed; merge conflicts with the base branch",
            "failing": [],
        }
    if merge_state == "BLOCKED":
        return {
            "state": "needs_owner",
            "reason": "Checks passed; blocked by branch protection "
            "(a human step is required)",
            "failing": [],
        }
    if merge_state in {"BEHIND", "UNKNOWN"}:
        return {
            "state": "green",
            "reason": f"Checks passed; merge state {merge_state}",
            "failing": [],
        }
    return {
        "state": "ready",
        "reason": "Checks passed on the current head; ready to merge",
        "failing": [],
    }


class PRTracker:
    def __init__(self, engine):
        self.engine = engine
        self.state = engine.state
        self.config = engine.config
        github = getattr(engine, "github", None)
        self.adapter = (
            github.adapter
            if github
            else GitHubAdapter(engine.home, engine.config)
        )
        self.adapter._state = self.state

    # -- scheduling ------------------------------------------------------

    def active(self):
        return bool(
            self.config.integration == "github"
            or self.config.tracked_repositories
            or list_prs(self.state, OPEN_PR_STATES)
        )

    def poll(self, *, force=False):
        if not self.active():
            return None
        row = self.state.db.execute(
            "SELECT value FROM settings WHERE key='pr_poll_at'"
        ).fetchone()
        if not force and row and float(row["value"]) > time.time():
            return None
        with self.state.db:
            self.state.db.execute(
                "INSERT OR REPLACE INTO settings VALUES ('pr_poll_at',?)",
                (str(time.time() + self.config.pr_poll_seconds),),
            )
        self.sweep()
        observed = 0
        for pr in list_prs(self.state, OPEN_PR_STATES):
            if pr["number"] is None:
                continue
            self.observe(pr)
            observed += 1
        return observed

    # -- discovery and reconciliation -----------------------------------

    def _list_open(self, slug):
        return self.adapter._json(
            [
                "gh",
                "pr",
                "list",
                "--repo",
                slug,
                "--state",
                "open",
                "--limit",
                "100",
                "--json",
                "number,title,url,headRefName,baseRefName,headRefOid",
            ]
        )

    def sweep(self):
        """Adopt every open factory-branch PR and listed-repo PR.

        Also completes intent rows whose PR was opened right before a crash.
        """
        scanned = {}
        for repo in workable_repositories(self.config):
            if repo.github:
                scanned[repo.github] = (repo.name, "factory")
        for tracked in self.config.tracked_repositories:
            scanned[tracked.repo] = (tracked.name, "tracked")
        for slug, (name, scope) in sorted(scanned.items()):
            try:
                rows = self._list_open(slug)
            except GitHubFailure as error:
                self._poll_failed(slug, error)
                continue
            for item in rows:
                self._adopt(slug, name, scope, item)
        for intent in pending_intents(self.state):
            self._reconcile_intent(intent)

    def _adopt(self, slug, name, scope, item):
        number = item["number"]
        if get_pr(self.state, slug, number) is not None:
            return
        branch = item.get("headRefName", "")
        intent = self.state.db.execute(
            "SELECT id FROM pull_requests WHERE slug=? AND branch=? "
            "AND number IS NULL",
            (slug, branch),
        ).fetchone()
        if intent:
            fill_number(
                self.state,
                intent["id"],
                number,
                item.get("url", ""),
                item.get("headRefOid"),
            )
            self.state.event(
                "pr.intent_reconciled",
                "factory",
                {"slug": slug, "number": number, "branch": branch},
            )
            return
        factory_branch = branch.startswith("factory/")
        if scope == "factory" and not factory_branch:
            return
        track_pr(
            self.state,
            slug,
            number,
            origin="factory" if factory_branch else "tracked",
            repo_name=name,
            title=item.get("title", ""),
            url=item.get("url", ""),
            branch=branch,
            base=item.get("baseRefName", ""),
            head=item.get("headRefOid", ""),
            event="pr.tracked",
        )
        if factory_branch:
            self.state.event(
                "pr.adopted_untracked",
                "factory",
                {"slug": slug, "number": number, "branch": branch},
            )
            self.state.notify(
                None,
                "attention",
                f"Adopted untracked factory PR {slug}#{number}",
                f"pr-adopted:{slug}#{number}",
            )

    def _reconcile_intent(self, intent):
        try:
            found = self.adapter._json(
                [
                    "gh",
                    "pr",
                    "list",
                    "--repo",
                    intent["slug"],
                    "--head",
                    intent["branch"],
                    "--state",
                    "all",
                    "--limit",
                    "1",
                    "--json",
                    "number,url,headRefOid",
                ]
            )
        except GitHubFailure as error:
            self._poll_failed(intent["slug"], error)
            return
        if found:
            fill_number(
                self.state,
                intent["id"],
                found[0]["number"],
                found[0].get("url", ""),
                found[0].get("headRefOid"),
            )
            self.state.event(
                "pr.intent_reconciled",
                intent["work_id"] or "factory",
                {"slug": intent["slug"], "number": found[0]["number"]},
            )

    def _poll_failed(self, subject, error):
        self.state.event(
            "pr.poll_failed",
            "factory",
            {"subject": str(subject), "code": error.code},
            f"pr-poll-failed:{subject}:{int(time.time() // 3600)}",
        )

    # -- observation -----------------------------------------------------

    def view(self, row):
        return self.adapter._json(
            [
                "gh",
                "pr",
                "view",
                str(row["number"]),
                "--repo",
                row["slug"],
                "--json",
                PR_FIELDS,
            ]
        )

    def observe(self, row):
        row = self._observe(row)
        self._escalate(row)
        return row

    def _escalate(self, row):
        """A PR that needs the owner always gets a decision (advised later)."""
        if (
            not row
            or row["state"] != "needs_owner"
            or row["reason"].startswith("Repair budget exhausted")
        ):
            return
        advice = fallback_advice(row["reason"], pull_request=True)
        add_decision(
            self.state,
            kind="free_text",
            notify=False,  # set_pr already raised an attention notice
            question=terse(
                f"{row['slug']}#{row['number']}: {row['reason']}", 30
            ),
            context=(
                f"{row['slug']}#{row['number']} {row['title']}\n"
                f"{row['reason']}\n\n{row['log_excerpt'][-1500:]}"
            ),
            options=[
                {
                    "label": o.label,
                    "effect": o.effect,
                    "tradeoff": o.tradeoff,
                    "locus": o.locus,
                    "description": o.effect,
                    "recommended": o.recommended,
                }
                for o in advice.options
            ],
            why=advice.why,
            advice="pending",
            block_key=f"pr:{row['slug']}#{row['number']}:{row['head']}:{row['reason'][:80]}",
        )

    def _observe(self, row):
        try:
            view = self.view(row)
        except GitHubFailure as error:
            self._poll_failed(f"{row['slug']}#{row['number']}", error)
            return row
        updates = {
            key: view.get(source) or row[key]
            for key, source in (
                ("title", "title"),
                ("url", "url"),
                ("branch", "headRefName"),
                ("base", "baseRefName"),
            )
        }
        updates["merge_state"] = view.get("mergeStateStatus") or ""
        updates["review_decision"] = view.get("reviewDecision") or ""
        head = view.get("headRefOid") or row["head"]
        if head != row["head"]:
            updates.update(head=head, failing=[], log_excerpt="")
            if row["state"] in {"failed", "repairing", "needs_owner"}:
                updates.update(state="pending", reason="New head observed")
        set_pr(self.state, row, **updates)
        row = get_pr(self.state, row["slug"], row["number"])
        verdict = classify(view, row, self.config)
        if verdict["state"] == "failed":
            return self._failed(row, view, verdict)
        set_pr(
            self.state,
            row,
            state=verdict["state"],
            reason=verdict["reason"],
            failing=verdict["failing"],
            **({"log_excerpt": ""} if verdict["state"] != "failed" else {}),
        )
        return get_pr(self.state, row["slug"], row["number"])

    # -- failure and repair ---------------------------------------------

    def _failure_context(self, row, view):
        """Bounded log excerpt from the failed workflow runs."""
        runs = []
        for check in view.get("statusCheckRollup") or []:
            result = str(check.get("conclusion") or "").upper()
            match = RUN_ID.search(check.get("detailsUrl") or "")
            if result in FAILURE_RANK and match and match[1] not in runs:
                runs.append(match[1])
        pieces = []
        for run_id in runs[:LOG_RUNS]:
            try:
                result = self.adapter._run(
                    [
                        "gh",
                        "run",
                        "view",
                        run_id,
                        "--repo",
                        row["slug"],
                        "--log-failed",
                    ]
                )
            except GitHubFailure:
                continue
            pieces.append(ANSI.sub("", result.stdout or ""))
        text = "\n".join(pieces)
        return text[-EXCERPT_LIMIT:] if len(text) > EXCERPT_LIMIT else text

    def _failed(self, row, view, verdict):
        excerpt = (
            row["log_excerpt"] if row["head"] and row["log_excerpt"] else ""
        )
        if not excerpt:
            excerpt = self._failure_context(row, view)
        fixable = verdict["fixable"]
        why = verdict.get("why_not", "")
        if fixable and HUMAN_ONLY_LOG.search(excerpt):
            fixable, why = (
                False,
                "failure looks like missing secrets or permissions",
            )
        fields = {"failing": verdict["failing"], "log_excerpt": excerpt}
        if not fixable:
            set_pr(
                self.state,
                row,
                state="needs_owner",
                reason=f"{verdict['reason']} ({why})",
                **fields,
            )
            return get_pr(self.state, row["slug"], row["number"])
        if row["state"] not in {"failed", "repairing"}:
            set_pr(
                self.state,
                row,
                state="failed",
                reason=verdict["reason"],
                **fields,
            )
        else:
            set_pr(self.state, row, reason=verdict["reason"], **fields)
        row = get_pr(self.state, row["slug"], row["number"])
        return self._repair(row)

    def _stacked_on(self, row):
        """An open tracked PR whose branch is this PR's (non-default) base."""
        if row["base"] in {"", "main", "master"}:
            return None
        for other in list_prs(self.state, OPEN_PR_STATES):
            if other["slug"] == row["slug"] and other["branch"] == row["base"]:
                return other
        return None

    def _repair(self, row):
        stacked = self._stacked_on(row)
        if stacked:
            set_pr(
                self.state,
                row,
                reason=(
                    f"Stacked on #{stacked['number']}; not repairing until "
                    "its base has landed or been rebased"
                ),
            )
            return get_pr(self.state, row["slug"], row["number"])
        if self.state.is_paused():
            return row
        active = self._active_repair(row)
        if active == "wait":
            if row["state"] != "repairing":
                set_pr(self.state, row, state="repairing")
            return get_pr(self.state, row["slug"], row["number"])
        if active == "deferred":
            set_pr(
                self.state,
                row,
                state="needs_owner",
                reason="The repair worker deferred; see the open decision",
            )
            return get_pr(self.state, row["slug"], row["number"])
        attempts = row["repairs"].get(row["head"], 0)
        if (
            attempts >= self.config.pr_max_repairs
            or row["repair_total"] >= self.config.pr_max_repairs_total
        ):
            return self._exhausted(row, attempts)
        try:
            self._spawn(row, attempts)
        except StateConflict:
            return row
        return get_pr(self.state, row["slug"], row["number"])

    def _active_repair(self, row):
        """'wait' while a repair is in flight, 'deferred' if parked."""
        if not row["work_id"] and not row["repair_work"]:
            return None
        work_id = row["repair_work"] or row["work_id"]
        try:
            work = self.state.get_work(work_id)
        except ValueError:
            return None
        if row["origin"] == "factory" and row["work_id"]:
            # Factory-owned: the revision path is in flight unless the work
            # sits idle at awaiting_merge.
            if work["phase"] == "awaiting_merge":
                return None
        elif work["phase"] == "completed":
            return None
        if work["control"] in {"deferred", "quarantined"}:
            return "deferred"
        return "wait"

    def _exhausted(self, row, attempts):
        reason = (
            f"Repair budget exhausted ({attempts} on {row['head'][:8]}, "
            f"{row['repair_total']} total): {row['reason']}"
        )
        set_pr(self.state, row, state="needs_owner", reason=reason)
        self._pr_decision(row)
        return get_pr(self.state, row["slug"], row["number"])

    def _pr_decision(self, row):
        return add_decision(
            self.state,
            question=(
                f"{row['slug']}#{row['number']} still fails after repairs "
                f"on {row['head'][:8]}. Try again?"
            ),
            context=f"{row['reason']}\n\n{row['log_excerpt'][-1500:]}",
            kind="choice",
            options=[
                {
                    "label": "Grant another repair attempt",
                    "description": "Reset the repair budget for this PR",
                    "recommended": True,
                },
                {
                    "label": "I will fix it myself",
                    "description": "Leave the PR to the owner",
                },
            ],
            proposal={"pr": f"{row['slug']}#{row['number']}"},
        )

    def reject_repair(self, slug, number, reason):
        """A reviewer rejected a repair: it costs one attempt on this head.

        Returns True while budget remains (the worker revises); otherwise the
        PR moves to needs_owner with a decision and False is returned.
        """
        row = get_pr(self.state, slug, number)
        if row is None:
            return False
        repairs = dict(row["repairs"])
        repairs[row["head"]] = repairs.get(row["head"], 0) + 1
        total = row["repair_total"] + 1
        set_pr(
            self.state,
            row,
            repairs=repairs,
            repair_total=total,
            reason=f"Repair rejected in review: {reason[:300]}",
        )
        self.state.event(
            "pr.repair_rejected",
            row["work_id"] or "factory",
            {"pr": f"{slug}#{number}", "head": row["head"], "reason": reason},
        )
        row = get_pr(self.state, slug, number)
        if (
            repairs[row["head"]] >= self.config.pr_max_repairs
            or total >= self.config.pr_max_repairs_total
        ):
            self._exhausted(row, repairs[row["head"]])
            return False
        return True

    def grant(self, slug, number):
        """Owner granted more repairs: reset the budget and re-evaluate."""
        row = get_pr(self.state, slug, number)
        if row is None:
            return
        set_pr(
            self.state,
            row,
            state="failed",
            reason="Repair budget reset by the owner",
            repairs={},
            repair_total=0,
            repair_work=None,
        )

    def _context(self, row):
        return (
            f"CI failed on {row['slug']}#{row['number']} at head "
            f"{row['head'][:12]} ({row['reason']}).\n"
            f"Failure log excerpt:\n{row['log_excerpt']}\n\n{REPAIR_RULES}"
        )

    def _ordinal(self, row):
        """Next repair number for this PR; never reused, even after a grant."""
        prefix = repair_source(row["slug"], row["number"])
        (count,) = self.state.db.execute(
            "SELECT COUNT(*) FROM work WHERE source_key LIKE ? ESCAPE '\\'",
            (prefix.replace("_", "\\_") + ":%",),
        ).fetchone()
        return count + 1

    def _spawn(self, row, attempts):
        repairs = dict(row["repairs"])
        repairs[row["head"]] = attempts + 1
        fields = {
            "state": "repairing",
            "repairs": repairs,
            "repair_total": row["repair_total"] + 1,
        }
        if row["origin"] == "factory" and row["work_id"]:
            work = self.state.get_work(row["work_id"])
            if work["phase"] == "awaiting_merge" and not work["current_run"]:
                set_pr(self.state, row, **fields)
                self.engine._revise(work, self._context(row))
                self.state.event(
                    "pr.repair_requested",
                    row["work_id"],
                    {"slug": row["slug"], "number": row["number"]},
                )
            return
        repository = next(
            (
                repo
                for repo in workable_repositories(self.config)
                if repo.github == row["slug"]
            ),
            None,
        )
        if repository is None or not repository.checks:
            set_pr(
                self.state,
                row,
                state="needs_owner",
                reason=(
                    "Cannot repair: no local checkout with checks is "
                    f"configured for {row['slug']} "
                    "(add [[tracked_repositories]] local_path and checks)"
                ),
            )
            return
        work = self.state.submit(
            Submission(
                title=f"Repair {row['slug']}#{row['number']}: CI failing",
                instruction=(
                    f"Repair the failing CI of {row['slug']}#{row['number']} "
                    f"on branch {row['branch']} and push to the same branch."
                    f" Failure context is in the task packet. {REPAIR_RULES}"
                ),
                acceptance=[
                    "The failing checks named in the packet pass locally",
                    "No tests, lint rules or assertions were weakened",
                ],
                repositories=[repository.name],
                source=repair_source(row["slug"], row["number"]),
                delivery_key=f"{row['head']}:{self._ordinal(row)}",
            )
        )
        set_pr(self.state, row, repair_work=work["id"], **fields)
        self.state.event(
            "pr.repair_requested",
            work["id"],
            {"slug": row["slug"], "number": row["number"], "head": row["head"]},
        )
