"""Durable phase transitions around interchangeable interactive workers."""

import json
import time
from pathlib import Path
from threading import Event

from pydantic import ValidationError

from .config import atomic_json, digest, load_config, resolved_bundle
from .coordinator import Coordinator, CoordinatorError
from .git import GitFailure, GitWorkspace
from .github import GitHubFailure
from .harness import HarnessError, make_harness
from .improvements import propose
from .models import Failure, WorkerReport
from .notifications import flush
from .recovery import plan_recovery
from .state import State, StateConflict, identity


class Engine:
    def __init__(self, home: Path, *, harness=None, coordinator=None):
        self.home = Path(home).resolve()
        self.config = load_config(self.home)
        self.state = State(self.home)
        self.git = GitWorkspace(self.home, self.config)
        self.harness = harness or make_harness(self.config)
        self.coordinator = coordinator or Coordinator(self.home, self.config)
        self.github = None
        if self.config.integration == "github":
            from .github_flow import GitHubFlow

            self.github = GitHubFlow(self.state, self.config)

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

    def tick(self):
        if self.state.is_paused():
            return False
        work_items = self.state.list_work()
        active = [
            work
            for work in work_items
            if self._ready(work) and work["current_run"]
        ]
        ready = active or [work for work in work_items if self._ready(work)]
        if not ready:
            return False
        work = ready[0]
        try:
            self._step(work)
        except StateConflict:
            self.state.event("operation.superseded", work["id"], {})
        except CoordinatorError as error:
            self._failure(work["id"], error.failure)
        except HarnessError as error:
            self._failure(work["id"], Failure.model_validate(error.failure))
        except (GitFailure, GitHubFailure) as error:
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
        return True

    def run(self, once=False, max_seconds=None, watch=None):
        started = time.monotonic()
        watching = bool(self.github) if watch is None else watch
        with self.state.writer():
            self.state.event(
                "coordinator.started",
                "factory",
                {
                    "harness": self.config.harness,
                    "coordinator": self.config.coordinator,
                },
            )
            while not self.state.is_paused():
                if max_seconds is not None and (
                    time.monotonic() - started >= max_seconds
                ):
                    break
                if self.github:
                    self.github.poll(force=once)
                progressed = self.tick()
                if once:
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
                if not progressed:
                    future = [
                        item["wake_at"]
                        for item in pending
                        if item["wake_at"] > time.time()
                    ]
                    if not future and not watching:
                        break
                    delay = (
                        max(min(future) - time.time(), 0.01) if future else 1
                    )
                    Event().wait(min(delay, 1))
            self.flush_notifications()
        return self.state.inspect()

    def _step(self, work):
        if work["current_run"]:
            self._collect(work)
            return
        work_id = work["id"]
        data = work["data"]
        phase = work["phase"]
        if phase == "queued":
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
            self._dispatch(work, role)
        elif phase == "checking":
            operation = self.state.begin_operation(work_id, "git.snapshot", {})
            data["entries"] = self.git.snapshot(
                data["entries"], data.get("workspace_id", work_id)
            )
            self.state.finish_operation(operation, {"entries": data["entries"]})
            self.state.update_work(work_id, data=data)
            self._validate(work_id, "checks", "reviewing")
        elif phase == "preparing":
            if self.github:
                self.github.adapter.refresh_defaults(data["entries"])
            data["entries"], changed = self.git.sync(data["entries"])
            self.state.update_work(
                work_id,
                data=data,
                phase="checking" if changed else "qa_checks",
                control="ready",
                event="candidate.synchronized",
            )
        elif phase == "qa_checks":
            self._validate(work_id, "qa", "qa")
        elif phase == "integrating":
            self._integrate(work)
        elif phase == "awaiting_merge" and self.github:
            self.github.advance(work, self._revise, self._defer)
        else:
            raise ValueError(f"unsupported phase {phase}")

    def _dispatch(self, work, role):
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
        }
        Path(assignment["session_dir"]).mkdir(parents=True, exist_ok=True)
        Path(assignment["role_path"]).write_text(bundle["prompts"][role])
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
            "prior_reports": work["data"].get("reports", {}),
            "pull_requests": work["data"].get("pull_requests", []),
            "revision_reason": work["data"].get("revision_reason"),
            "evidence": [json.loads(row["data"]) for row in evidence],
            "qa_guidance": bundle["qa_guidance"],
            "report_contract": WorkerReport.model_json_schema(),
        }
        Path(assignment["packet_path"]).write_text(
            "# Factory task packet\n\n" + json.dumps(packet, indent=2) + "\n"
        )
        run = self.state.create_run(work_id, role, assignment)
        atomic_json(Path(assignment["assignment_path"]), run["assignment"])
        self._launch(run)

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

    def _collect(self, work):
        run = self.state.get_run(work["current_run"])
        assignment = run["assignment"]
        if run["session"] is None:
            self._launch(run)
            return
        observation = self.harness.observe(run["session"], assignment)
        self.state.event(
            "worker.observed",
            work["id"],
            {
                "run_id": run["id"],
                **observation,
            },
        )
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
                    Failure(
                        code="report.missing",
                        summary="Worker settled without a valid report",
                        source="runtime",
                    ),
                )
        elif time.time() - run["created"] > self.config.phase_timeout_seconds:
            self._failure(
                work["id"],
                Failure(
                    code="phase.timeout",
                    summary="Assignment exceeded its phase deadline",
                    source="coordinator",
                ),
            )

    def _decide(self, work, allowed, preferred, report=None, evaluation=None):
        bundle = resolved_bundle(self.home, self.config)
        snapshot = {
            "work": work["payload"],
            "phase": work["phase"],
            "report": report,
            "preferred": preferred,
            "recoveries": work["recoveries"],
            "revisions": work["revisions"],
            "candidate": work["data"].get("entries"),
            "evaluation": evaluation,
        }
        if evaluation in {"acceptance", "handoff"}:
            snapshot["evidence"] = self._current_evidence(work)
        if evaluation == "handoff":
            next_phase = {
                "implementing": "checking",
                "reviewing": "preparing",
                "qa": "integrating",
            }[work["phase"]]
            advance_meanings = {
                "checking": (
                    "Snapshot the implementation and execute independent "
                    "checks; this grants neither integration nor completion."
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

    def _current_evidence(self, work):
        candidates = {
            entry["name"]: entry["candidate"]
            for entry in work["data"]["entries"]
        }
        current_digest = digest(resolved_bundle(self.home, self.config))
        rows = self.state.db.execute(
            "SELECT id,data FROM evidence WHERE work_id=?", (work["id"],)
        ).fetchall()
        results = []
        for row in rows:
            result = json.loads(row["data"])
            if (
                result.get("candidate_set") == candidates
                and result.get("config_digest") == current_digest
                and result.get("passed")
            ):
                results.append({"id": row["id"], **result})
        return results

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
            run["role"] == "implementer"
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
        data.setdefault("sessions", {})[run["role"]] = run["session"]
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
        if action in {"revise", "retry"}:
            self._revise(work, rationale)
        elif action == "defer":
            self._defer(work, rationale)
        else:
            phase = {
                "implementer": "checking",
                "reviewer": "preparing",
                "qa": "integrating",
            }[run["role"]]
            self.state.update_work(
                work["id"],
                phase=phase,
                control="ready",
                wake_at=0,
                event="work.advanced",
            )

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
        entries, changed = self.git.sync(data["entries"])
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
        decision = self._decide(
            work,
            {"advance", "complete", "revise", "defer"},
            "advance" if remaining else "complete",
            evaluation="acceptance",
        )
        if decision.action == "revise":
            self._revise(work, decision.rationale)
            return
        if decision.action == "defer":
            self._defer(work, decision.rationale)
            return
        evidence_ids = {item["id"] for item in self._current_evidence(work)}
        expected = set(range(len(work["payload"]["acceptance"])))
        assessments = {item.criterion: item for item in decision.acceptance}
        if decision.action == "complete":
            if (
                remaining
                or set(assessments) != expected
                or (len(assessments) != len(decision.acceptance))
            ):
                raise ValueError(
                    "completion lacks a complete acceptance assessment"
                )
            for assessment in assessments.values():
                if (
                    assessment.result != "passed"
                    or not assessment.evidence_refs
                    or not set(assessment.evidence_refs).issubset(evidence_ids)
                ):
                    raise ValueError(
                        "acceptance cites absent or stale evidence"
                    )
        elif not remaining:
            remaining = [
                item.explanation
                for item in decision.acceptance
                if item.result != "passed"
            ] or [decision.rationale]
        data["acceptance_decision"] = decision.model_dump()
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

    def _revise(self, work, reason):
        if work["revisions"] >= self.config.max_revisions:
            self._defer(work, "Revision budget exhausted: " + reason)
            return
        data = work["data"]
        data["revision_reason"] = reason
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
                "incident_key": f"deferred:{work['id']}:{work['generation']}",
            },
        )

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
        }
        if (
            failure.code not in known_codes
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
            try:
                if run["session"]:
                    observation = self.harness.observe(
                        run["session"], run["assignment"]
                    )
                    if observation["state"] not in {"settled", "finished"}:
                        result = self.harness.interrupt(run["session"])
                        self.state.event("worker.interrupted", work_id, result)
                        observation = self.harness.observe(
                            run["session"], run["assignment"]
                        )
                        if observation["state"] not in {"settled", "finished"}:
                            self.harness.close(run["session"])
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
                self.state.event(
                    "recovery.action_failed", work_id, error.failure
                )
                return
            if failure.source != "coordinator" or run["report"] is None:
                self.state.update_run(run_id, state="failed")
                run_id = None
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

    def resume(self, work_id=None):
        return self.state.resume(work_id)

    def flush_notifications(self):
        return flush(self.state, self.config)
