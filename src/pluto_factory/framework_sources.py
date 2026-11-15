"""Read-only Pluto framework sources for the advisor.

Libraries are resolved at the versions the apps pin: Go modules from the
module cache (via each registered repository's go.mod), TypeScript packages
from node_modules, plus optional local checkouts from ``[advisor]
reference_paths``. Nothing here writes; readers are jailed to the resolved
roots.
"""

import json
import os
import re
import subprocess
from pathlib import Path

GO_REQUIRE = re.compile(
    r"^\s*(?:require\s+)?(github\.com/plutoframework/[A-Za-z0-9_.-]+)"
    r"(?:/v\d+)?\s+(v\S+)",
    re.MULTILINE,
)
IGNORED_MODULES = {"pluto-factory"}
SKIP_DIRS = {".git", "node_modules", "__pycache__", "dist", "build"}
SECRET_NAMES = re.compile(
    r"(^\.env|\.pem$|\.key$|id_rsa|credentials)", re.IGNORECASE
)
MAX_FILE = 20000
MAX_HITS = 40


def _mod_cache():
    configured = os.environ.get("GOMODCACHE")
    if configured:
        return Path(configured)
    try:
        result = subprocess.run(
            ["go", "env", "GOMODCACHE"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if result.returncode == 0 and result.stdout.strip():
            return Path(result.stdout.strip())
    except (OSError, subprocess.SubprocessError):
        pass
    return Path.home() / "go" / "pkg" / "mod"


def _escape(module):
    return re.sub(r"[A-Z]", lambda m: "!" + m[0].lower(), module)


def _checkouts(config):
    seen = []
    for repo in [
        *config.repositories,
        *getattr(config, "tracked_repositories", []),
    ]:
        raw = getattr(repo, "path", None) or getattr(repo, "local_path", "")
        if raw and Path(raw).is_dir() and Path(raw) not in seen:
            seen.append(Path(raw))
    return seen


def resolve(config) -> list[dict]:
    """[{library, path, version, origin}] for every reachable source."""
    found = {}

    def add(library, path, version, origin):
        try:
            real = Path(path).expanduser().resolve(strict=True)
        except OSError:
            return
        if real.is_dir():
            found.setdefault(
                str(real),
                {
                    "library": library,
                    "path": str(real),
                    "version": version,
                    "origin": origin,
                },
            )

    cache = None
    for checkout in _checkouts(config):
        go_mod = checkout / "go.mod"
        if go_mod.is_file():
            cache = cache or _mod_cache()
            for module, version in GO_REQUIRE.findall(go_mod.read_text()):
                name = module.rsplit("/", 1)[-1]
                if name not in IGNORED_MODULES:
                    add(
                        name,
                        cache / f"{_escape(module)}@{version}",
                        version,
                        "go-module-cache",
                    )
        modules = checkout / "node_modules"
        for scope in (modules / "@plutoframework",):
            for package in sorted(scope.glob("*")) if scope.is_dir() else []:
                add(
                    f"@plutoframework/{package.name}",
                    package,
                    _package_version(package),
                    "node_modules",
                )
        for package in sorted(modules.glob("pluto-*")):
            add(
                package.name, package, _package_version(package), "node_modules"
            )
    for raw in config.advisor.reference_paths:
        path = Path(raw).expanduser()
        add(path.name, path, "local", "reference_paths")
    return sorted(found.values(), key=lambda item: item["library"])


def _package_version(path):
    try:
        return json.loads((Path(path) / "package.json").read_text())["version"]
    except (OSError, ValueError, KeyError):
        return "unknown"


def library_names(sources) -> set[str]:
    names = set()
    for item in sources:
        name = item["library"].lower()
        names.add(name)
        names.add(name.removeprefix("@plutoframework/"))
    return names


def pins(sources) -> dict[str, str]:
    return {item["library"]: item["version"] for item in sources}


def digest(sources) -> list[dict]:
    """Small listing for the packet: what exists and at which version."""
    result = []
    for item in sources:
        root = Path(item["path"])
        docs = [
            p.name
            for p in sorted(root.glob("*.md"))[:6]
            if not SECRET_NAMES.search(p.name)
        ]
        result.append(
            {
                "library": item["library"],
                "version": item["version"],
                "docs": docs,
            }
        )
    return result


class ReferenceReader:
    """List, read and search framework sources; cannot leave its roots."""

    def __init__(self, sources):
        self.roots = {
            item["library"]: Path(item["path"]).resolve() for item in sources
        }

    def _jail(self, library, relative=""):
        root = self.roots.get(library)
        if root is None:
            raise ValueError(f"unknown library {library!r}")
        target = (root / relative).resolve()
        if not target.is_relative_to(root):
            raise ValueError("path escapes the library")
        if SECRET_NAMES.search(target.name):
            raise ValueError("refused: sensitive file name")
        return root, target

    def list_dir(self, library, relative=""):
        _, target = self._jail(library, relative)
        return sorted(
            child.name + ("/" if child.is_dir() else "")
            for child in target.iterdir()
            if child.name not in SKIP_DIRS
            and not SECRET_NAMES.search(child.name)
        )[:200]

    def read_file(self, library, relative):
        _, target = self._jail(library, relative)
        if not target.is_file():
            raise ValueError("not a file")
        return target.read_text(errors="replace")[:MAX_FILE]

    def search(self, library, pattern):
        root, _ = self._jail(library)
        regex = re.compile(pattern)
        hits = []
        for path in sorted(root.rglob("*")):
            if (
                len(hits) >= MAX_HITS
                or not path.is_file()
                or SKIP_DIRS & set(path.relative_to(root).parts)
                or SECRET_NAMES.search(path.name)
                or path.stat().st_size > 1_000_000
            ):
                continue
            try:
                lines = path.read_text().splitlines()
            except (OSError, UnicodeDecodeError):
                continue
            for number, line in enumerate(lines, 1):
                if regex.search(line):
                    hits.append(
                        f"{path.relative_to(root)}:{number}: {line[:200]}"
                    )
                    if len(hits) >= MAX_HITS:
                        break
        return hits
