"""Typed model decisions constrained to coordinator-supplied legal actions."""

import asyncio
import json
import os
from pathlib import Path

from .credentials import credential_environment
from .models import CriterionAssessment, Decision, FactoryConfig, Failure

CLAUDE_TIMEOUT_SECONDS = 120


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
        payload = json.dumps(
            {"allowed_actions": sorted(allowed), "snapshot": snapshot}
        )
        try:
            if self.config.coordinator_runtime == "claude_agent_sdk":
                decision = self._claude_decide(prompt, payload)
            else:
                decision = self._pydantic_decide(prompt, payload)
        except Exception as error:
            raise CoordinatorError(
                Failure(
                    code=_failure_code(error),
                    summary=f"Coordinator model operation failed ({type(error).__name__})",
                    source="coordinator",
                )
            ) from error
        if decision.action not in allowed:
            raise ValueError("model proposed an action outside the legal set")
        return decision

    def _pydantic_decide(self, prompt: str, payload: str) -> Decision:
        from pydantic_ai import Agent

        credentials = credential_environment(self.home)
        previous = {key: os.environ.get(key) for key in credentials}
        try:
            os.environ.update(credentials)
            agent = Agent(
                self.config.coordinator_model,
                output_type=Decision,
                instructions=prompt,
                retries=1,
                model_settings={"timeout": 45},
            )
            return agent.run_sync(payload).output
        finally:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def _claude_decide(self, prompt: str, payload: str) -> Decision:
        """Decide through the Claude Agent SDK using the local Claude login.

        No tools and no filesystem settings are loaded, so the session is a
        pure structured-output call billed to the user's Claude account.
        """
        from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, query

        model = self.config.coordinator_model.removeprefix("anthropic:")
        options = ClaudeAgentOptions(
            model=model,
            system_prompt=prompt,
            tools=[],
            setting_sources=[],
            max_turns=3,
            cwd=str(self.home),
            output_format={
                "type": "json_schema",
                "schema": Decision.model_json_schema(),
            },
        )

        async def run() -> ResultMessage | None:
            result = None
            async for message in query(prompt=payload, options=options):
                if isinstance(message, ResultMessage):
                    result = message
            return result

        result = asyncio.run(asyncio.wait_for(run(), CLAUDE_TIMEOUT_SECONDS))
        if result is None:
            raise ClaudeCoordinatorError("no result from Claude Agent SDK")
        if result.is_error or result.structured_output is None:
            raise ClaudeCoordinatorError(
                result.subtype, status_code=result.api_error_status
            )
        return Decision.model_validate(result.structured_output)


class ClaudeCoordinatorError(RuntimeError):
    def __init__(self, detail: str, status_code: int | None = None):
        super().__init__(detail)
        self.status_code = status_code


def _failure_code(error: Exception) -> str:
    if type(error).__name__ == "CredentialError":
        return "auth.required"
    return {
        401: "auth.required",
        403: "auth.required",
        429: "provider.rate_limited",
    }.get(getattr(error, "status_code", None), "coordinator.failed")
