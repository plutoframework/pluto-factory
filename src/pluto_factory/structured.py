"""Schema-constrained, read-only model calls shared by advisor and triage."""

import asyncio
import os

from .credentials import credential_environment
from .framework_sources import ReferenceReader

TIMEOUT_SECONDS = 300
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


def claude_options(config, home, prompt, sources, model, reasoning, schema):
    """SDK options: read-only tools confined to the given source roots."""
    from claude_agent_sdk import ClaudeAgentOptions

    return ClaudeAgentOptions(
        model=model,
        system_prompt=prompt,
        tools=list(READ_ONLY_TOOLS),
        allowed_tools=list(READ_ONLY_TOOLS),
        disallowed_tools=list(DENIED_TOOLS),
        permission_mode="dontAsk",
        setting_sources=[],
        add_dirs=[item["path"] for item in sources],
        max_turns=12,
        cwd=str(home),
        effort=reasoning,
        output_format={"type": "json_schema", "schema": schema},
    )


def run(
    config, home, *, prompt, payload, output_type, sources, model, reasoning
):
    if config.coordinator_runtime == "claude_agent_sdk":
        return _claude(
            config,
            home,
            prompt,
            payload,
            output_type,
            sources,
            model,
            reasoning,
        )
    from pydantic_ai import Agent

    reader = ReferenceReader(sources)
    credentials = credential_environment(home)
    previous = {key: os.environ.get(key) for key in credentials}
    try:
        os.environ.update(credentials)
        agent = Agent(
            model,
            output_type=output_type,
            instructions=prompt,
            retries=1,
            model_settings={
                "timeout": 90,
                "openai_reasoning_effort": reasoning,
            },
        )

        @agent.tool_plain
        def list_dir(library: str, path: str = "") -> list[str]:
            """List a directory of a read-only source tree."""
            return reader.list_dir(library, path)

        @agent.tool_plain
        def read_file(library: str, path: str) -> str:
            """Read a source file (read-only)."""
            return reader.read_file(library, path)

        @agent.tool_plain
        def search(library: str, pattern: str) -> list[str]:
            """Regex-search a source tree (read-only)."""
            return reader.search(library, pattern)

        return agent.run_sync(payload).output
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _claude(
    config, home, prompt, payload, output_type, sources, model, reasoning
):
    from claude_agent_sdk import ResultMessage, query

    options = claude_options(
        config,
        home,
        prompt,
        sources,
        model,
        reasoning,
        output_type.model_json_schema(),
    )

    async def go():
        result = None
        async for message in query(prompt=payload, options=options):
            if isinstance(message, ResultMessage):
                result = message
        return result

    result = asyncio.run(asyncio.wait_for(go(), TIMEOUT_SECONDS))
    if result is None or result.is_error or result.structured_output is None:
        raise RuntimeError("no structured output")
    return result.structured_output
