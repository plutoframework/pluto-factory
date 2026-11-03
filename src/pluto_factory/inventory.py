"""Read-only factory inventory; never instantiate or migrate runtime state."""

import fcntl
import json
import math
import os
import shutil
import sqlite3
import stat
import textwrap
import tomllib
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory

from .models import FactoryConfig


def _clean(value) -> str:
    return " ".join(
        "".join(
            character if character.isprintable() else " "
            for character in str(value)
        ).split()
    )


def _wrapped(value: str, width: int, indent: str = "") -> list[str]:
    return textwrap.wrap(
        _clean(value),
        width=width,
        initial_indent=indent,
        subsequent_indent=indent,
        break_long_words=True,
        break_on_hyphens=False,
    ) or [indent]


def _model_summary(models: dict) -> str:
    values = []
    if models.get("coordinator"):
        values.append(f"coordinator={models['coordinator']}")
    grouped = {}
    for role in ("implementer", "reviewer", "qa"):
        profile = models.get(role)
        if not isinstance(profile, dict):
            continue
        model = profile.get("model", "unknown")
        if profile.get("provider"):
            model = f"{profile['provider']}:{model}"
        if profile.get("reasoning"):
            model += f" ({profile['reasoning']})"
        grouped.setdefault(model, []).append(role)
    values.extend(
        f"{'/'.join(roles)}={model}" for model, roles in grouped.items()
    )
    return "; ".join(values) or "unavailable"


def render_inventory(inventory: dict, width: int | None = None) -> str:
    """Human list view; machine identifiers remain available through --json."""
    width = max(
        20, min(width or shutil.get_terminal_size((88, 24)).columns, 120)
    )
    entries = inventory.get("factories", [])
    lines = [f"Factories ({len(entries)})"]
    lines.extend(
        _wrapped(f"Registry: {inventory.get('root', 'unknown')}", width)
    )
    lines.append("")
    if not entries:
        lines.extend(_wrapped("No factories registered.", width))
    table = width >= 72
    name_width = min(24, width // 4)
    columns = (name_width, 16, 18, width - name_width - 40)
    if entries and table:
        lines.append(
            "  ".join(
                label.ljust(size)
                for label, size in zip(
                    ("NAME", "STATUS", "PHASE", "REPOSITORIES"),
                    columns,
                    strict=True,
                )
            ).rstrip()
        )
        lines.append("-" * width)
    for entry in entries:
        status = entry.get("status", "unknown")
        if entry.get("coordinator_running") and status != "running":
            status += " (running)"
        repositories = (
            ", ".join(
                repo.get("name", "unknown")
                for repo in entry.get("repositories", [])
            )
            or "unavailable"
        )
        phase = entry.get("current_phase") or "idle"
        if table:
            cells = [
                _wrapped(str(value), size)
                for value, size in zip(
                    (entry.get("name", "unknown"), status, phase, repositories),
                    columns,
                    strict=True,
                )
            ]
            for row in range(max(map(len, cells))):
                lines.append(
                    "  ".join(
                        (cell[row] if row < len(cell) else "").ljust(size)
                        for cell, size in zip(cells, columns, strict=True)
                    ).rstrip()
                )
        else:
            lines.extend(_wrapped(entry.get("name", "unknown"), width))
            lines.extend(
                _wrapped(f"Status: {status}; phase: {phase}", width, "  ")
            )
            lines.extend(_wrapped(f"Repositories: {repositories}", width, "  "))
        lines.extend(
            _wrapped(
                f"Models: {_model_summary(entry.get('models', {}))}",
                width,
                "  ",
            )
        )
        lines.extend(
            _wrapped(f"Path: {entry.get('location', 'unknown')}", width, "  ")
        )
        activity = entry.get("last_activity") or "not recorded"
        if activity != "not recorded":
            try:
                activity = (
                    datetime.fromisoformat(activity)
                    .astimezone()
                    .strftime("%b %d %Y, %H:%M %Z")
                )
            except ValueError:
                activity = "unavailable"
        lines.extend(_wrapped(f"Activity: {activity}", width, "  "))
        for error in entry.get("errors", []):
            lines.extend(_wrapped(f"Warning: {error}", width, "  "))
        lines.append("")
    for error in inventory.get("errors", []):
        lines.extend(_wrapped(f"Warning: {error}", width))
    return "\n".join(lines).rstrip()


def _running(home: Path) -> tuple[bool | None, str | None]:
    lock = home / "coordinator.lock"
    if not lock.exists() and not lock.is_symlink():
        return False, None
    descriptor = None
    try:
        descriptor = os.open(lock, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            return None, "coordinator lock is not a regular file"
        try:
            fcntl.flock(descriptor, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            return True, None
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        return False, None
    except OSError:
        return None, "coordinator lock could not be inspected"
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _signature(paths: list[Path]) -> tuple:
    result = []
    for path in paths:
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            result.append(None)
            continue
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError("state files must be regular files")
        result.append((metadata.st_ino, metadata.st_size, metadata.st_mtime_ns))
    return tuple(result)


def _number(value) -> float | None:
    if isinstance(value, (int, float)) and math.isfinite(value) and value >= 0:
        return float(value)
    return None


def _read_snapshot(database: Path, include_sessions: bool = False) -> dict:
    connection = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only = ON")
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version != 1:
            raise ValueError("unsupported inventory database schema")
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if not {"work", "settings", "events"}.issubset(tables):
            raise ValueError("incomplete inventory database schema")
        setting = connection.execute(
            "SELECT value FROM settings WHERE key='paused'"
        ).fetchone()
        rows = [
            dict(row)
            for row in connection.execute(
                "SELECT id,title,phase,control,current_run,generation,updated FROM work "
                "ORDER BY created,id"
            )
        ]
        latest = []
        for table, column in (
            ("work", "updated"),
            ("runs", "updated"),
            ("events", "created"),
            ("operations", "updated"),
            ("evidence", "created"),
            ("notifications", "created"),
            ("improvements", "created"),
        ):
            if table not in tables:
                continue
            value = _number(
                connection.execute(
                    f"SELECT MAX({column}) FROM {table}"
                ).fetchone()[0]
            )
            if value is not None:
                latest.append(value)
        sessions = {}
        if include_sessions and "runs" in tables:
            allowed = {
                "kind",
                "address",
                "core_identity",
                "assignment_id",
                "factory",
                "owned",
                "work_id",
                "role",
                "generation",
            }
            for row in connection.execute(
                "SELECT id,work_id,generation,role,state,session FROM runs "
                "WHERE id IN (SELECT current_run FROM work WHERE current_run IS NOT NULL)"
            ):
                session = json.loads(row["session"]) if row["session"] else None
                if session is not None and not isinstance(session, dict):
                    raise ValueError("invalid session metadata")
                sessions[row["id"]] = {
                    key: row[key]
                    for key in ("id", "work_id", "generation", "role", "state")
                } | {
                    "session": {
                        key: value
                        for key, value in session.items()
                        if key in allowed
                    }
                    if session
                    else None
                }
        return {
            "paused": bool(setting and setting["value"] == "true"),
            "work": rows,
            "last_activity": max(latest) if latest else None,
            "sessions": sessions,
        }
    finally:
        connection.close()


def _state(home: Path, include_sessions: bool = False) -> dict | None:
    database = home / "state.sqlite3"
    if not database.exists() and not database.is_symlink():
        return None
    files = [database, home / "state.sqlite3-wal"]
    # Even SQLite mode=ro can create WAL/SHM files. Read a stable private copy
    # so inventory never changes the source factory's files or locks its DB.
    with TemporaryDirectory(prefix="pluto-factory-list-") as temporary:
        for attempt in range(3):
            before = _signature(files)
            copied = Path(temporary) / str(attempt)
            copied.mkdir()
            try:
                for source, metadata in zip(files, before, strict=True):
                    if metadata is not None:
                        shutil.copyfile(source, copied / source.name)
                after = _signature(files)
            except FileNotFoundError:
                continue
            if before == after:
                return _read_snapshot(copied / database.name, include_sessions)
    raise RuntimeError("factory state changed during inventory snapshot")


def _configuration(home: Path) -> tuple[FactoryConfig | None, str]:
    path = home / "factory.toml"
    if not path.is_file():
        return None, "missing"
    try:
        with path.open("rb") as stream:
            # Inventory retains registered paths even if a checkout is offline.
            return FactoryConfig.model_validate(tomllib.load(stream)), "valid"
    except (ValueError, TypeError, OSError):
        return None, "invalid"


def _status(running: bool | None, state: dict | None, state_error: bool) -> str:
    if state and state["paused"]:
        return "paused"
    if running is True:
        return "running"
    if running is None:
        return "unknown"
    if state_error:
        return "unavailable"
    if state is None or not state["work"]:
        return "registered"
    unfinished = [
        work for work in state["work"] if work["phase"] != "completed"
    ]
    if unfinished and all(work["control"] == "paused" for work in unfinished):
        return "paused"
    if unfinished and all(
        work["control"] in {"deferred", "quarantined"} for work in unfinished
    ):
        return "deferred"
    return "stopped"


def describe_factory(home: Path) -> dict:
    home = home.resolve()
    config, configuration_status = _configuration(home)
    running, lock_error = _running(home)
    errors = []
    if configuration_status != "valid":
        errors.append(f"factory configuration is {configuration_status}")
    if lock_error:
        errors.append(lock_error)
    state_error = False
    try:
        state = _state(home)
    except (sqlite3.Error, OSError, ValueError, RuntimeError):
        state = None
        state_error = True
        errors.append("factory database is unavailable, invalid, or changing")
    work = state["work"] if state else []
    unfinished = [item for item in work if item["phase"] != "completed"]
    eligible = [
        item
        for item in unfinished
        if item["control"] not in {"deferred", "quarantined", "paused"}
    ]
    current = next((item for item in eligible if item["current_run"]), None)
    if current is None:
        current = next(
            (item for item in eligible if item["phase"] != "queued"), None
        )
    if current is None:
        current = next(iter(eligible), None)
    if current is None and running is not True:
        current = next(iter(unfinished), None)
    last_activity = state["last_activity"] if state else None
    activity_source = "ledger" if last_activity is not None else None
    try:
        configured_at = (home / "factory.toml").stat().st_mtime
        if last_activity is None or configured_at > last_activity:
            last_activity = configured_at
            activity_source = "configuration"
    except OSError:
        pass
    try:
        timestamp = (
            datetime.fromtimestamp(last_activity, UTC).isoformat(
                timespec="seconds"
            )
            if last_activity is not None
            else None
        )
    except (ValueError, OverflowError, OSError):
        timestamp = None
        activity_source = None
    status = _status(running, state, state_error)
    if config is None and status in {"registered", "stopped"}:
        status = "unavailable"
    return {
        "name": config.name if config else home.name,
        "location": str(home),
        "status": status,
        "coordinator_running": running,
        "configuration_status": configuration_status,
        "state_status": "unavailable"
        if state_error
        else ("present" if state else "missing"),
        "harness": config.harness if config else None,
        "integration": config.integration if config else None,
        "repositories": [
            {
                "name": repo.name,
                "path": repo.path,
                "access": repo.access,
                "github": repo.github,
            }
            for repo in config.repositories
        ]
        if config
        else [],
        "models": {
            "coordinator": config.coordinator_model
            if config.coordinator == "model"
            else "rules",
            "implementer": config.worker.model_dump(),
            "reviewer": config.reviewer.model_dump(),
            "qa": config.qa_model.model_dump(),
        }
        if config
        else {},
        "current_phase": current["phase"]
        if current
        else ("idle" if running else None),
        "current_work": {
            key: current[key] for key in ("id", "title", "phase", "control")
        }
        if current
        else None,
        "work_count": len(work),
        "unfinished_count": len(unfinished),
        "deferred_count": sum(
            item["control"] in {"deferred", "quarantined"}
            for item in unfinished
        ),
        "paused_count": sum(item["control"] == "paused" for item in unfinished),
        "last_activity": timestamp,
        "last_activity_source": activity_source,
        "errors": errors,
    }


def list_factories(root: Path, extra_home: Path | None = None) -> dict:
    root = root.expanduser().resolve()
    homes = []
    errors = []
    registry_exists = False
    try:
        if root.is_dir():
            registry_exists = True
            homes.extend(
                path
                for path in root.iterdir()
                if path.is_dir()
                and any(
                    (path / name).exists()
                    for name in (
                        "factory.toml",
                        "state.sqlite3",
                        "coordinator.lock",
                    )
                )
            )
        elif root.exists():
            errors.append("factory registry location is not a directory")
    except OSError:
        errors.append("factory registry could not be read")
    if extra_home is not None:
        selected = extra_home.expanduser().resolve()
        if selected.is_dir():
            homes.append(selected)
        else:
            errors.append("explicit factory home does not exist")
    entries = []
    for home in sorted({path.resolve() for path in homes}, key=str):
        try:
            entries.append(describe_factory(home))
        except (OSError, ValueError):
            entries.append(
                {
                    "name": home.name,
                    "location": str(home),
                    "status": "unavailable",
                    "errors": ["factory metadata could not be read"],
                }
            )
    return {
        "root": str(root),
        "registry_exists": registry_exists,
        "factories": sorted(
            entries, key=lambda item: (item["name"], item["location"])
        ),
        "errors": errors,
    }
