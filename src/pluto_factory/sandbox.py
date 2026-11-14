"""Run workers inside a nono sandbox hosted in their Herdr terminal.

The factory still drives the terminal from the host (``hc send``/``read``);
nothing inside the sandbox can reach the Herdr socket, because the profile
must mediate pathname AF_UNIX sockets. Without that, any sandboxed process
could ``hc exec`` its way out. The worker sees only its worktrees (git
metadata read-only except its own index), its assignment folder, its
session folder, its agent's own config and any explicitly granted paths.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

PROFILE = Path(__file__).with_name("nono_sealed.json")


class SandboxError(RuntimeError):
    pass


def profile_path(policy: dict) -> str:
    value = (policy or {}).get("profile") or ""
    return os.path.expanduser(value) if value else str(PROFILE)


def herdr_socket() -> str:
    return os.environ.get("HC_TERMINAL_SOCKET") or (
        f"/run/user/{os.getuid()}/herdr-core/default.sock"
    )


def check(policy: dict) -> str | None:
    """Return why sandboxed dispatch cannot work, or ``None``."""
    if not shutil.which("nono"):
        return "nono is not installed (https://nono.sh)"
    profile = profile_path(policy)
    try:
        verdict = subprocess.run(
            [
                "nono",
                "why",
                "--profile",
                profile,
                "--unix-socket",
                herdr_socket(),
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        return f"nono profile check failed: {error}"
    first = (verdict.stdout.strip().splitlines() or [""])[0]
    if first != "DENIED":
        return (
            f"nono profile {profile!r} does not block the Herdr socket; "
            "extend the bundled profile or set "
            'linux.af_unix_mediation = "pathname"'
        )
    return None


def resolve_binary(name: str) -> Path:
    """The real executable: the sandbox must see past mise shims."""
    found = shutil.which(name)
    if not found:
        raise SandboxError(f"{name} is not installed")
    real = Path(found).resolve()
    if real.name == "mise":
        result = subprocess.run(
            ["mise", "which", name],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if result.returncode or not result.stdout.strip():
            raise SandboxError(f"mise cannot resolve {name}")
        real = Path(result.stdout.strip()).resolve()
    return real


def _git_dirs(path: str) -> tuple[str, str] | None:
    result = subprocess.run(
        [
            "git",
            "-C",
            path,
            "rev-parse",
            "--path-format=absolute",
            "--git-common-dir",
            "--git-dir",
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    lines = result.stdout.split()
    if result.returncode or len(lines) != 2:
        return None
    return lines[0], lines[1]


def _claude_grants(read: list, write: list, files: list) -> None:
    configured = os.environ.get("CLAUDE_CONFIG_DIR")
    folder = Path(configured or Path.home() / ".claude").expanduser()
    state = (
        folder / ".claude.json" if configured else Path.home() / ".claude.json"
    )
    folder.mkdir(parents=True, exist_ok=True)
    write.append(folder)
    if state.exists():
        files.append(state)
    # Claude refuses to start when its temp folder is unreadable.
    temporary = Path(f"/tmp/claude-{os.getuid()}")
    temporary.mkdir(mode=0o700, exist_ok=True)
    write.append(temporary)
    # settings.json, CLAUDE.md, skills... are often symlinks out of ~/.claude.
    for child in folder.iterdir():
        if child.is_symlink() and child.exists():
            target = child.resolve()
            read.append(target if target.is_dir() else target.parent)


def _pi_grants(read: list, write: list, files: list) -> None:
    configured = os.environ.get("PI_CODING_AGENT_DIR")
    folder = Path(configured or Path.home() / ".pi" / "agent").expanduser()
    folder.mkdir(parents=True, exist_ok=True)
    write.append(folder)
    for key in ("PI_CODING_AGENT_SESSION_DIR", "PI_PACKAGE_DIR"):
        if os.environ.get(key):
            read.append(Path(os.environ[key]).expanduser())


def grants(assignment: dict, agent: str, policy: dict) -> dict:
    read: list[Path] = [
        Path(sys.prefix),
        Path(sys.base_prefix),
        Path(__file__).resolve().parent,
    ]
    write: list[Path] = [
        Path(assignment["assignment_path"]).parent,
        Path(assignment["session_dir"]),
    ]
    files: list[Path] = []
    for folder in write:
        folder.mkdir(parents=True, exist_ok=True)
    binary = resolve_binary(agent)
    # mise and npm installs keep runtime files beside the binary.
    read.append(binary.parent.parent)
    {"claude": _claude_grants, "pi": _pi_grants}[agent](read, write, files)
    for root in assignment.get("roots", []):
        (write if root["access"] == "write" else read).append(
            Path(root["path"])
        )
        dirs = _git_dirs(root["path"])
        if dirs:
            common, own = dirs
            read.append(Path(common))
            # The worktree's own admin folder holds its index and HEAD.
            if root["access"] == "write" and own != common:
                write.append(Path(own))
    read.extend(Path(p).expanduser() for p in policy.get("read", []))
    write.extend(Path(p).expanduser() for p in policy.get("write", []))

    def unique(paths):
        seen = []
        for path in paths:
            text = str(path)
            if text not in seen and path.exists():
                seen.append(text)
        return seen

    writes = unique(write)
    return {
        "binary": str(binary),
        "read": [p for p in unique(read) if p not in writes],
        "write": writes,
        "write_files": unique(files),
    }


def wrap(
    assignment: dict, agent: str, policy: dict, native: list[str]
) -> list[str]:
    """``nono run`` argv that executes ``native`` (argv[0] = agent name)."""
    allowed = grants(assignment, agent, policy)
    command = [
        "nono",
        "run",
        "--silent",
        "--profile",
        profile_path(policy),
        "--name",
        f"pf-{assignment['id']}"[:32],
        "--allow-cwd",
    ]
    for path in allowed["write"]:
        command.extend(["--allow", path])
    for path in allowed["read"]:
        command.extend(["--read", path])
    for path in allowed["write_files"]:
        command.extend(["--allow-file", path])
    for domain in policy.get("allow_domains", []):
        command.extend(["--allow-domain", domain])
    return [*command, "--", allowed["binary"], *native[1:]]
