"""GitHub intake and PR waiting are independent of worker execution."""

import json
import time

from .github import GitHubAdapter, GitHubFailure
from .models import Submission


class GitHubFlow:
    def __init__(self, state, config):
        self.state = state
        self.config = config
        self.adapter = GitHubAdapter(state.home, config)

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
                accepted = Submission.model_validate(
                    json.loads(existing["payload"])
                ).model_dump()
                if accepted != submission.model_dump():
                    self.state.event(
                        "intake.issue_changed",
                        existing["id"],
                        {
                            "note": "Existing accepted scope was preserved",
                        },
                    )
                continue
            try:
                self.state.submit(submission)
            except ValueError as error:
                self._reject(source_key, str(error))

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
        if self.config.github_auto_merge:
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

    def advance(self, work, revise, defer):
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
            work = self.state.update_work(work["id"], data=data)
            revise(work, "GitHub review or checks require changes")
            return
        if status == "closed":
            defer(work, "A required pull request was closed without merging")
            return
        if status != "merged":
            if self.config.github_auto_merge:
                self.adapter.request_merge(result["pull_requests"])
            self.state.update_work(
                work["id"],
                data=data,
                control="backoff",
                wake_at=time.time() + self.config.github_poll_seconds,
            )
            return
        self.adapter.refresh_defaults(data["entries"])
        remaining = data.get("remaining_after_merge", [])
        if remaining:
            data["increment"] = data.get("increment", 0) + 1
            data["revision_reason"] = "; ".join(remaining)
            for key in ("entries", "reports", "pull_requests"):
                data.pop(key, None)
            self.state.update_work(
                work["id"],
                phase="queued",
                control="ready",
                data=data,
                wake_at=0,
                event="increment.merged",
            )
        else:
            if self.config.close_source_issues:
                self.adapter.complete_sources(work)
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
