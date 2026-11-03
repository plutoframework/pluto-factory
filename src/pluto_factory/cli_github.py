"""Local setup helpers for GitHub intake; remote work belongs to the adapter."""

import re
import shutil
import subprocess
from pathlib import Path
from urllib.parse import urlsplit

from .models import FactoryConfig


def github_slug(value: str) -> str:
    if not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*", value
    ):
        raise ValueError("GitHub repositories must use OWNER/REPOSITORY")
    return value


def origin_slug(path: Path) -> str:
    result = subprocess.run(
        ["git", "-C", str(path), "remote", "get-url", "origin"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        return ""
    remote = result.stdout.strip()
    if remote.startswith("git@github.com:"):
        candidate = remote.removeprefix("git@github.com:")
    else:
        parsed = urlsplit(remote)
        if (
            parsed.scheme not in {"https", "ssh"}
            or parsed.hostname != "github.com"
            or parsed.query
            or parsed.fragment
        ):
            return ""
        candidate = parsed.path.lstrip("/")
    candidate = candidate.rstrip("/").removesuffix(".git")
    try:
        return github_slug(candidate)
    except ValueError:
        return ""


def mappings(values: list[str]) -> dict[str, str]:
    result = {}
    for value in values:
        name, separator, slug = value.partition("=")
        if not separator or not name or name in result:
            raise ValueError(
                "GitHub mappings use unique NAME=OWNER/REPOSITORY entries"
            )
        result[name] = github_slug(slug)
    return result


def resolve_repositories(
    config: FactoryConfig, values: list[str]
) -> FactoryConfig:
    selected = mappings(values)
    names = {repo.name for repo in config.repositories}
    if selected.keys() - names:
        raise ValueError("a GitHub mapping names an unregistered repository")
    repositories = []
    for repo in config.repositories:
        slug = selected.get(repo.name, repo.github)
        if not slug and config.integration == "github":
            slug = origin_slug(Path(repo.path))
        if slug:
            slug = github_slug(slug)
        if (
            config.integration == "github"
            and repo.access == "write"
            and not slug
        ):
            raise ValueError(
                "GitHub integration requires a slug for every writable repository"
            )
        repositories.append(repo.model_copy(update={"github": slug}))
    return config.model_copy(update={"repositories": repositories})


def readiness(
    config: FactoryConfig, verify_auth: bool = False
) -> tuple[list[dict], dict]:
    if config.integration != "github":
        return [], {"enabled": False, "authentication": "not_applicable"}
    available = shutil.which("gh") is not None
    checks = [{"name": "executable:gh", "passed": available}]
    checks.extend(
        {
            "name": f"github-mapping:{repo.name}",
            "passed": bool(repo.github),
        }
        for repo in config.repositories
        if repo.access == "write"
    )
    authentication = "not_verified"
    if verify_auth:
        authenticated = False
        if available:
            try:
                result = subprocess.run(
                    ["gh", "auth", "status", "--hostname", "github.com"],
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=20,
                )
                authenticated = result.returncode == 0
            except (OSError, subprocess.TimeoutExpired):
                authenticated = False
        checks.append(
            {"name": "github-authentication", "passed": authenticated}
        )
        authentication = "verified" if authenticated else "unavailable"
    return checks, {"enabled": True, "authentication": authentication}
