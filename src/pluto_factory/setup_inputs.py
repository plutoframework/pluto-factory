"""Small setup input conveniences shared by presentation and validators."""

import re
from pathlib import Path


def factory_name(value: str) -> str:
    name = re.sub(r"\s+", "-", value.strip().lower())
    if not re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", name):
        raise ValueError(
            "factory name needs 1-32 lowercase letters, digits, - or _"
        )
    return name


def repository_choice(value: str, cwd: Path | None = None) -> str:
    if "=" in value:
        return value
    path = Path(value).expanduser() if value else (cwd or Path.cwd())
    path = path.resolve()
    name = re.sub(r"[^a-z0-9_-]", "-", path.name.lower())
    if not name or not name[0].isalpha():
        name = "repo-" + name
    return f"{name[:32]}={path}"
