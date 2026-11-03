"""Factory configuration is editable TOML; resolved runs pin its digest."""

import hashlib
import json
import os
import tomllib
from pathlib import Path

from .models import FactoryConfig

ROLE_TEXT = {
    "orchestrator": (
        "Choose a permitted next action from recorded evidence. Recover "
        "autonomously within authority. Notifications do not require blocking. "
        "An unsupported claim never substitutes for required evidence."
    ),
    "implementer": (
        "Implement the assigned increment in the listed worktrees. Preserve "
        "deployability and keep unfinished features disabled. Follow repository "
        "instructions. Do not push, merge, change factory policy, or start "
        "other agents. Return the final result with factory_report."
    ),
    "reviewer": (
        "Independently review the assigned candidate and acceptance criteria. "
        "Do not change source, push, merge, or start agents. Inspect every "
        "assigned repository. Report required defects as findings and use "
        "factory_report for the verdict."
    ),
    "qa": (
        "Validate the candidate's assigned behavior using the supplied "
        "scenarios and recorded checks. Do not change source or start agents. "
        "Report missing or contradictory evidence honestly with factory_report."
    ),
}


def factory_root() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))
    return Path(base) / "pluto" / "factories"


def load_config(home: Path) -> FactoryConfig:
    with (home / "factory.toml").open("rb") as stream:
        config = FactoryConfig.model_validate(tomllib.load(stream))
    names = [repo.name for repo in config.repositories]
    if len(names) != len(set(names)):
        raise ValueError("repository names must be unique")
    paths = [
        str(Path(repo.path).resolve(strict=True))
        for repo in config.repositories
    ]
    if len(paths) != len(set(paths)):
        raise ValueError("a repository cannot be registered twice")
    return config


def resolved_bundle(home: Path, config: FactoryConfig) -> dict:
    prompts = {}
    for role in ROLE_TEXT:
        path = home / "prompts" / f"{role}.md"
        prompts[role] = path.read_text()
    guidance = home / "qa" / "guidance.md"
    rules_path = home / "qa" / "rules.toml"
    rules = tomllib.loads(rules_path.read_text())
    expected_rules = {
        "schema_version": 1,
        "require_checks": True,
        "require_qa": True,
        "required_findings_block_integration": True,
    }
    if rules != expected_rules:
        raise ValueError(
            "unsupported QA gate rules; required gates were preserved"
        )
    return {
        "config": config.model_dump(),
        "prompts": prompts,
        "qa_guidance": guidance.read_text() if guidance.exists() else "",
        "qa_rules": rules,
    }


def digest(value: dict) -> str:
    content = json.dumps(value, sort_keys=True).encode()
    return hashlib.sha256(content).hexdigest()


def atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("w") as stream:
        json.dump(value, stream, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def write_config(home: Path, config: FactoryConfig) -> None:
    if (home / "factory.toml").exists():
        raise ValueError("factory already exists; configuration was preserved")
    home.mkdir(parents=True, exist_ok=True)
    home.chmod(0o700)
    for folder in (
        "sessions",
        "artifacts",
        "repositories",
        "worktrees",
        "assignments",
        "revisions",
        "logs",
        "evaluations",
    ):
        (home / folder).mkdir(exist_ok=True)
    scalar = config.model_dump(
        exclude={
            "repositories",
            "worker",
            "reviewer",
            "qa_model",
            "self_improvement",
        }
    )
    lines = [f"{key} = {json.dumps(value)}" for key, value in scalar.items()]
    for table in ("worker", "reviewer", "qa_model", "self_improvement"):
        lines.extend(["", f"[{table}]"])
        for key, value in getattr(config, table).model_dump().items():
            lines.append(f"{key} = {json.dumps(value)}")
    for repo in config.repositories:
        lines.extend(["", "[[repositories]]"])
        for key, value in repo.model_dump(
            exclude={"flag_environments"}
        ).items():
            lines.append(f"{key} = {json.dumps(value)}")
        environments = repo.flag_environments
        for environment in environments:
            lines.extend(["", "[[repositories.flag_environments]]"])
            for key, value in environment.items():
                lines.append(f"{json.dumps(key)} = {json.dumps(value)}")
    (home / "factory.toml").write_text("\n".join(lines) + "\n")
    for role, text in ROLE_TEXT.items():
        path = home / "prompts" / f"{role}.md"
        path.parent.mkdir(exist_ok=True)
        path.write_text(text + "\n")
    (home / "qa").mkdir(exist_ok=True)
    (home / "qa" / "guidance.md").write_text(
        "Validate declared acceptance scenarios and enabled/disabled flags.\n"
    )
    (home / "qa" / "rules.toml").write_text(
        "schema_version = 1\n"
        "require_checks = true\n"
        "require_qa = true\n"
        "required_findings_block_integration = true\n"
    )
