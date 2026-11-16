"""GitHub intake and PR waiting are independent of worker execution."""

import copy
import time

from .acceptance import acceptance_intent, citable_ids, current_evidence
from .config import digest, resolved_bundle
from .github import GitHubAdapter, GitHubFailure
from .models import Decision, Submission
from .state import StateConflict


class ConfigurationDrift(ValueError):
    """Merged candidates are intact; only the configuration digest moved."""


class GitHubFlow:
    def __init__(self, state, config):
        self.state = state
        self.config = config
        self.adapter = GitHubAdapter(state.home, config)
        self.adapter._state = state

    def poll(self, *, force=False):
        row = self.state.db.execute(
            "SELECT value FROM settings WHERE key='github_poll_at'"
        ).fetchone()
        if not force and row and float(row["value"]) > time.time():
            return
        with self.state.db:
            self.state.db.execute(
                "INSERT OR REPLACE INTO settings VALUES ('github_poll_at',?)",
                (str(time.time() + self.config.github_poll_seconds),),
            )
        try:
            submissions = self.adapter.poll()
        except GitHubFailure as error:
            self.state.event(
                "intake.failed",
                "github",
                {
                    "code": error.code,
                    "summary": error.summary,
                },
            )
            self.state.notify(
                None,
                "progress",
                "GitHub intake will retry; other work continues",
                f"intake:{int(time.time() // 300)}",
            )
            return
        for rejection in getattr(self.adapter, "rejected_issues", []):
            self._reject(rejection["source_key"], rejection["summary"])
        for submission in submissions:
            source_key = f"{submission.source}:{submission.delivery_key}"
            existing = self.state.db.execute(
                "SELECT id,payload FROM work WHERE source_key=?", (source_key,)
            ).fetchone()
            if existing:
                self._observe_change(existing, submission)
                continue
            try:
                self.state.submit(submission)
            except ValueError as error:
                self._reject(source_key, str(error))

    def _observe_change(self, existing, submission):
        """Record each distinct edited issue state once; scope stays frozen."""
        work = self.state.get_work(existing["id"])
        accepted = Submission.model_validate(
            self.state.original_payload(work)
        ).model_dump()
        observed = submission.model_dump()
        if accepted == observed:
            return
        observed_digest = digest(observed)
        data = work["data"]
        if data.get("observed_issue_digest") == observed_digest:
            return
        changed = {
            field: {
                "accepted": digest({"v": accepted[field]})[:12],
                "observed": digest({"v": observed[field]})[:12],
            }
            for field in observed
            if accepted.get(field) != observed[field]
        }
        change = {
            "digest": observed_digest,
            "changed": changed,
            "observed": {
                key: observed[key]
                for key in ("title", "instruction", "acceptance")
                if key in changed
            },
            "surfaced": False,
        }

        def record(current):
            current["observed_issue_digest"] = observed_digest
            current["issue_change"] = change

        try:
            self.state.update_work(
                existing["id"],
                mutate=record,
                event="intake.issue_observed",
            )
        except StateConflict:
            return  # paused; recorded on a later poll
        self.state.event(
            "intake.issue_changed",
            existing["id"],
            {
                "note": "Existing accepted scope was preserved",
                "changed": sorted(changed),
                "digest": observed_digest,
            },
        )

    def _reject(self, source_key, reason):
        self.state.event(
            "intake.issue_rejected",
            source_key,
            {"reason": reason[:500]},
            f"intake-rejected:{source_key}:{reason[:100]}",
        )
        self.state.notify(
            None,
            "action_required",
            "Issue declaration needs correction; other work continues",
            f"dependency-rejected:{source_key}",
        )

    def publish(self, work, entries, remaining):
        data = work["data"]
        data["publication_acceptance"] = self._publication_snapshot(work)
        for key in ("post_merge_acceptance", "post_merge_validation"):
            data.pop(key, None)
        work = self.state.update_work(
            work["id"], data=data, event="acceptance.publication_frozen"
        )
        operation = self.state.begin_operation(
            work["id"],
            "github.publish",
            {
                "candidate_set": {
                    entry["name"]: entry["candidate"] for entry in entries
                }
            },
        )
        pull_requests = self.adapter.publish(work, entries)
        self.state.finish_operation(operation, {"pull_requests": pull_requests})
        data = work["data"]
        data["pull_requests"] = pull_requests
        data["remaining_after_merge"] = remaining
        if self.config.github_auto_merge and not self.queue_active:
            self.adapter.request_merge(pull_requests)
        self.state.update_work(
            work["id"],
            phase="awaiting_merge",
            control="backoff",
            data=data,
            wake_at=time.time() + self.config.github_poll_seconds,
            event="pull_requests.published",
            notice={
                "kind": "progress",
                "message": "Validated PRs opened: "
                + ", ".join(pr["url"] for pr in pull_requests),
                "incident_key": f"prs:{work['id']}:{work['generation']}",
            },
        )

    @property
    def queue_active(self):
        """The merge queue serializes same-repository merges when parallel."""
        return self.config.concurrency.parallel

    def queue_heads(self, work):
        """slug -> True when this work's PR is first in that repo's queue.

        The queue is every work item awaiting merge with an open PR in the
        repository, oldest item first. Only the head merges (or is offered
        to the owner) and resyncs on the newest base; the rest wait.
        """
        if not self.queue_active:
            return {}
        slugs = {
            pr.get("slug")
            for pr in work["data"].get("pull_requests", [])
            if pr.get("state", "open") == "open"
        }
        heads = {}
        for slug in slugs:
            for other in self.state.list_work():
                if other["phase"] != "awaiting_merge" or other["control"] in {
                    "paused",
                    "deferred",
                    "quarantined",
                }:
                    continue
                if any(
                    pr.get("slug") == slug and pr.get("state", "open") == "open"
                    for pr in other["data"].get("pull_requests", [])
                ):
                    heads[slug] = other["id"] == work["id"]
                    break
            else:
                heads[slug] = True
        return heads

    def _ready_for_owner(self, work, pull_requests):
        """Auto-merge is off: announce the queue head once per PR head."""
        ready = [
            pr
            for pr in pull_requests
            if pr.get("state") == "open" and pr.get("merge_state") == "CLEAN"
        ]
        for pr in ready:
            self.state.notify(
                work["id"],
                "progress",
                f"Next in the merge queue for {pr.get('slug')}: "
                f"{pr.get('url', '')}",
                f"merge-queue-ready:{pr.get('slug')}#{pr.get('number')}:"
                f"{pr.get('head')}",
            )

    def advance(
        self,
        work,
        revise,
        defer,
        reassess=None,
        confirm=None,
        revalidate=None,
        resync=None,
    ):
        result = self.adapter.poll_pull_requests(work["data"]["pull_requests"])
        data = work["data"]
        data["pull_requests"] = result["pull_requests"]
        self.state.event("pull_requests.observed", work["id"], result)
        if any(pr.get("head_changed") for pr in result["pull_requests"]):
            defer(
                work,
                "A PR head changed outside the validated candidate; revalidation required",
            )
            return
        status = result["state"]
        if status == "changes_requested":
            reviewed = any(
                pr.get("review_decision") == "CHANGES_REQUESTED"
                for pr in result["pull_requests"]
            )
            if not reviewed:
                # Failing checks only: the PR tracker owns CI repair (bounded
                # per head SHA, with the failure log) and revises this work.
                self.state.update_work(
                    work["id"],
                    data=data,
                    control="backoff",
                    wake_at=time.time() + self.config.github_poll_seconds,
                )
                return
            work = self.state.update_work(work["id"], data=data)
            revise(work, "GitHub review requires changes")
            return
        if status == "closed":
            defer(work, "A required pull request was closed without merging")
            return
        if status != "merged":
            heads = self.queue_heads(work)
            stale = [
                pr
                for pr in result["pull_requests"]
                if pr.get("state") == "open"
                and (
                    pr.get("merge_state") == "DIRTY"
                    or (
                        pr.get("merge_state") == "BEHIND"
                        and (
                            self.config.github_require_up_to_date
                            # Queue head only: followers resync when they
                            # reach the front, on whatever base is newest.
                            or heads.get(pr.get("slug"))
                        )
                    )
                )
            ]
            if stale and resync is not None:
                work = self.state.update_work(work["id"], data=data)
                resync(work, stale)
                return
            if self.queue_active and not all(
                heads.get(pr.get("slug"), True)
                for pr in result["pull_requests"]
                if pr.get("state") == "open"
            ):
                waiting = sorted(
                    slug for slug, head in heads.items() if not head
                )
                self.state.event(
                    "merge_queue.waiting",
                    work["id"],
                    {"repositories": waiting},
                    f"merge-queue-wait:{work['id']}:{','.join(waiting)}",
                )
            elif self.config.github_auto_merge:
                self.adapter.request_merge(result["pull_requests"])
            elif self.queue_active:
                self._ready_for_owner(work, result["pull_requests"])
            self.state.update_work(
                work["id"],
                data=data,
                control="backoff",
                wake_at=time.time() + self.config.github_poll_seconds,
            )
            return
        try:
            decision, remaining = self._merged_intent(work, publication=True)
        except ConfigurationDrift as error:
            if revalidate is None:
                self._reconcile(work, defer, error)
                return
            work = self.state.update_work(
                work["id"], data=data, event="validation.configuration_drifted"
            )
            revalidated = revalidate(work)
            if revalidated is None:
                return
            if (
                not isinstance(revalidated, dict)
                or revalidated.get("id") != work["id"]
            ):
                raise ValueError(
                    "post-merge revalidation returned another item"
                )
            work = revalidated
            data = work["data"]
            try:
                decision, remaining = self._merged_intent(
                    work, publication=True
                )
            except (KeyError, OSError, TypeError, ValueError) as error:
                self._reconcile(work, defer, error)
                return
        except (KeyError, OSError, TypeError, ValueError) as error:
            self._reconcile(work, defer, error)
            return
        work = self.state.update_work(
            work["id"], data=data, event="pull_requests.merged"
        )
        if reassess is not None:
            reassessed = reassess(work)
            if reassessed is None:
                return
            if (
                not isinstance(reassessed, dict)
                or reassessed.get("id") != work["id"]
            ):
                raise ValueError(
                    "post-merge reassessment returned another item"
                )
            work = reassessed
            data = work["data"]
        elif remaining:
            defer(
                work,
                "Merged PRs need a fresh acceptance assessment before "
                "starting another coding increment",
            )
            return
        self._current_work(work)
        try:
            self._merged_intent(work)
            self.adapter.refresh_defaults(data["entries"])
            decision, remaining = self._merged_intent(work)
        except (KeyError, OSError, TypeError, ValueError) as error:
            self._reconcile(work, defer, error)
            return
        self._current_work(work)
        work = self.state.update_work(
            work["id"], data=data, event="acceptance.merged_confirmed"
        )
        if confirm is not None:
            confirmed = confirm(work)
            if confirmed is None:
                return
            if (
                not isinstance(confirmed, dict)
                or confirmed.get("id") != work["id"]
            ):
                raise ValueError(
                    "post-merge confirmation returned another item"
                )
            work = confirmed
            self._current_work(work)
            try:
                decision, remaining = self._merged_intent(work)
            except (KeyError, OSError, TypeError, ValueError) as error:
                self._reconcile(work, defer, error)
                return
            data = work["data"]
        if remaining:
            self.continue_after_merge(work, decision, remaining)
        else:
            if self.config.close_source_issues:
                closure = self.adapter.complete_sources(work)
                expected_closure = (
                    "closed"
                    if work["payload"]["source"].startswith("github:")
                    else "not_github"
                )
                if (
                    not isinstance(closure, dict)
                    or closure.get("state") != expected_closure
                ):
                    defer(
                        work,
                        "Merged PRs need completion reconciliation: "
                        "source issue closure was not confirmed",
                    )
                    return
            self.state.update_work(
                work["id"],
                phase="completed",
                control="settled",
                data=data,
                event="work.completed",
                notice={
                    "kind": "completed",
                    "message": f"{work['title']}: all required PRs merged",
                    "incident_key": f"completed:{work['id']}",
                },
            )

    def _current_work(self, work):
        current = self.state.get_work(work["id"])
        if (
            current["updated"] != work["updated"]
            or current["phase"] != "awaiting_merge"
            or current["control"] == "paused"
            or self.state.is_paused()
        ):
            raise StateConflict("work changed during post-merge acceptance")

    def _reconcile(self, work, defer, error):
        self._current_work(work)
        work = self.state.update_work(
            work["id"],
            data=work["data"],
            event="completion.reconciliation_required",
        )
        defer(work, "Merged PRs need completion reconciliation: " + str(error))

    def continue_after_merge(self, work, decision, remaining):
        """Start genuine remaining scope on a new namespace, retaining merges."""
        self._current_work(work)
        if not remaining or any(
            not isinstance(item, str) or not item.strip() for item in remaining
        ):
            raise ValueError("post-merge continuation needs concrete scope")
        data = work["data"]
        history = data.setdefault("merged_increments", [])
        increment = data.get("increment", 0)
        if not any(item["increment"] == increment for item in history):
            history.append(
                {
                    "increment": increment,
                    "candidates": data["acceptance_candidate"],
                    "config_digest": data["acceptance_config_digest"],
                    "acceptance_decision": decision.model_dump(),
                    "publication_acceptance": copy.deepcopy(
                        data.get("publication_acceptance")
                    ),
                    "pull_requests": data["pull_requests"],
                }
            )
        data["increment"] = (
            max(increment, *(item["increment"] for item in history)) + 1
        )
        data["revision_reason"] = "; ".join(remaining)
        data.pop("conflict_revision", None)
        for key in (
            "entries",
            "reports",
            "pull_requests",
            "pr_resyncs",
            "publication_acceptance",
            "post_merge_acceptance",
            "post_merge_validation",
        ):
            data.pop(key, None)
        return self.state.update_work(
            work["id"],
            phase="queued",
            control="ready",
            current_run=None,
            data=data,
            wake_at=0,
            event="increment.merged",
        )

    @staticmethod
    def _publication_snapshot(work):
        data = work["data"]
        return copy.deepcopy(
            {
                "decision": data["acceptance_decision"],
                "candidates": data["acceptance_candidate"],
                "config_digest": data["acceptance_config_digest"],
            }
        )

    def _merged_intent(self, work, *, publication=False):
        """Revalidate frozen publication or current completion independently."""
        data = work["data"]
        if publication:
            if "publication_acceptance" not in data:
                if any(
                    key in data
                    for key in (
                        "post_merge_acceptance",
                        "post_merge_validation",
                    )
                ):
                    raise ValueError(
                        "original publication acceptance is missing"
                    )
                data["publication_acceptance"] = self._publication_snapshot(
                    work
                )
            acceptance = data["publication_acceptance"]
        else:
            acceptance = self._publication_snapshot(work)
        registered = {repo.name: repo for repo in self.config.repositories}
        scope = set(work["payload"]["repositories"])
        entries = {entry["name"]: entry for entry in data["entries"]}
        if (
            not scope
            or not scope.issubset(registered)
            or set(entries) != scope
            or len(entries) != len(data["entries"])
        ):
            raise ValueError("candidate repository coverage is incomplete")
        candidates = {
            name: entry["candidate"] for name, entry in entries.items()
        }
        required = {
            name for name in scope if registered[name].access == "write"
        }
        prs = {pr["repository"]: pr for pr in data["pull_requests"]}
        if (
            not required
            or set(prs) != required
            or len(prs) != len(data["pull_requests"])
        ):
            raise ValueError("required merged PR repository coverage differs")
        for name, pr in prs.items():
            entry = entries[name]
            if (
                pr.get("state") != "merged"
                or pr.get("head_changed") is not False
                or pr.get("head") != candidates[name]
                or pr.get("observed_head") != candidates[name]
                or pr.get("branch") != entry["branch"]
                or pr.get("base") != entry["default_branch"]
                or pr.get("slug") != registered[name].github
            ):
                raise ValueError(
                    "merged PR identity lacks candidate provenance"
                )
        if acceptance["candidates"] != candidates:
            raise ValueError("acceptance candidate is stale")
        for kind in ("checks", "qa"):
            if data.get(f"{kind}_candidate") != candidates:
                raise ValueError(f"{kind} candidate is stale")
        for role in ("reviewer", "qa"):
            if data.get("reports", {}).get(role, {}).get("outcome") != "ready":
                raise ValueError(f"{role} report is absent")
        current_digest = digest(resolved_bundle(self.state.home, self.config))
        revalidation = data.get("post_merge_revalidation") or {}
        rebound = (
            publication
            and revalidation.get("config_digest") == current_digest
            and revalidation.get("candidates") == candidates
            and revalidation.get("previous_config_digest")
            == acceptance["config_digest"]
        )
        bound = {
            "acceptance": current_digest
            if rebound
            else acceptance["config_digest"],
            **{
                kind: data.get(f"{kind}_config_digest")
                for kind in ("checks", "qa")
            },
            **{
                f"{role} report": data.get("report_config_digests", {}).get(
                    role
                )
                for role in ("reviewer", "qa")
            },
        }
        stale = sorted(
            name for name, value in bound.items() if value != current_digest
        )
        if stale:
            raise ConfigurationDrift(
                "configuration is stale since the merged candidates were "
                "validated (" + ", ".join(stale) + ")"
            )
        decision = Decision.model_validate(acceptance["decision"])
        if decision.action not in {"advance", "complete"}:
            raise ValueError(
                "recorded acceptance does not authorize integration"
            )
        evidence_ids = citable_ids(
            current_evidence(self.state, self.state.home, self.config, work)
        )
        if rebound:
            # The frozen publication decision was validated under the earlier
            # configuration; its citations stay valid against that digest.
            evidence_ids |= citable_ids(
                current_evidence(
                    self.state,
                    self.state.home,
                    self.config,
                    work,
                    config_digest=acceptance["config_digest"],
                )
            )
        intent, remaining = acceptance_intent(
            decision, len(work["payload"]["acceptance"]), evidence_ids
        )
        if intent.action == "complete" and decision.action != "complete":
            raise ValueError("terminal acceptance intent was not recorded")
        return intent, remaining
