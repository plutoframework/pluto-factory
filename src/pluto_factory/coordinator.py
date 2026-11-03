"""Typed model decisions constrained to coordinator-supplied legal actions."""

import json
import os
from pathlib import Path

from .credentials import credential_environment
from .models import CriterionAssessment, Decision, FactoryConfig, Failure


class CoordinatorError(RuntimeError):
    def __init__(self, failure: Failure):
        super().__init__(failure.summary)
        self.failure = failure


class Coordinator:
    def __init__(self, home: Path, config: FactoryConfig):
        self.home = home
        self.config = config

    def decide(
        self, snapshot: dict, allowed: set[str], prompt: str
    ) -> Decision:
        if self.config.coordinator == "rules":
            preferred = snapshot.get("preferred", "advance")
            action = preferred if preferred in allowed else min(allowed)
            assessments = []
            if snapshot.get("evaluation") == "acceptance":
                evidence = [item["id"] for item in snapshot["evidence"]]
                assessments = [
                    CriterionAssessment(
                        criterion=index,
                        result="passed",
                        evidence_refs=evidence,
                        explanation="Deterministic fixture acceptance checks passed",
                    )
                    for index, _ in enumerate(snapshot["work"]["acceptance"])
                ]
            return Decision(
                action=action,
                rationale="Explicit fixture/rules policy",
                acceptance=assessments,
            )
        previous = {}
        try:
            from pydantic_ai import Agent

            credentials = credential_environment(self.home)
            previous = {key: os.environ.get(key) for key in credentials}
            os.environ.update(credentials)
            agent = Agent(
                self.config.coordinator_model,
                output_type=Decision,
                instructions=prompt,
                retries=1,
                model_settings={"timeout": 45},
            )
            decision = agent.run_sync(
                json.dumps(
                    {
                        "allowed_actions": sorted(allowed),
                        "snapshot": snapshot,
                    }
                )
            ).output
        except Exception as error:
            status = getattr(error, "status_code", None)
            code = {
                401: "auth.required",
                403: "auth.required",
                429: "provider.rate_limited",
            }.get(status, "coordinator.failed")
            if type(error).__name__ == "CredentialError":
                code = "auth.required"
            raise CoordinatorError(
                Failure(
                    code=code,
                    summary=f"Coordinator model operation failed ({type(error).__name__})",
                    source="coordinator",
                )
            ) from error
        finally:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
        if decision.action not in allowed:
            raise ValueError("model proposed an action outside the legal set")
        return decision
