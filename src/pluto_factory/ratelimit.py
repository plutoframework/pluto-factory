"""Recognise provider usage/rate limits and when they lift.

A limit is not a worker or item fault: it is retried after the reset, never
counted against recoveries, and pauses new dispatches factory-wide.
"""

import json
import re
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .models import Failure

FALLBACK_SECONDS = 900.0
MAX_SECONDS = 8 * 24 * 3600.0
MARGIN_SECONDS = 60.0
TAIL_BYTES = 65536
TAIL_ENTRIES = 6

LIMIT = re.compile(
    r"hit your (?:[\w-]+ )?limit"
    r"|(?:5-hour|five-hour|weekly|usage|session|opus|sonnet)\s+limit\s+reached"
    r"|usage limit reached"
    r"|rate_limit_error|overloaded_error"
    r"|\b(?:429|529)\b.*(?:rate|limit|overload|too many)"
    r"|too many requests|API is overloaded",
    re.IGNORECASE,
)
EPOCH = re.compile(r"limit reached\|(\d{10})")
RESET = re.compile(
    r"resets?\s+(?:at\s+)?"
    r"(?:(?P<month>[A-Z][a-z]{2,8})\s+(?P<day>\d{1,2}),?\s+)?"
    r"(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?\s*(?P<ampm>[ap]m)"
    r"(?:\s*\((?P<tz>[A-Za-z_]+/[A-Za-z_]+)\))?",
    re.IGNORECASE,
)
RETRY_AFTER = re.compile(r"retry[- ]after\W+(\d+(?:\.\d+)?)", re.IGNORECASE)
MONTHS = {
    name: index
    for index, name in enumerate(
        ("jan", "feb", "mar", "apr", "may", "jun")
        + ("jul", "aug", "sep", "oct", "nov", "dec"),
        1,
    )
}


def is_limit(text: str) -> bool:
    return bool(LIMIT.search(text or ""))


def seconds_until_reset(text: str, now: float | None = None) -> float | None:
    """Seconds until the limit named in ``text`` lifts, or None if unstated."""
    now = time.time() if now is None else now
    if match := EPOCH.search(text):
        return _clamp(int(match[1]) - now)
    if match := RETRY_AFTER.search(text):
        return _clamp(float(match[1]))
    match = RESET.search(text)
    if not match:
        return None
    try:
        zone = ZoneInfo(match["tz"]) if match["tz"] else None
    except (KeyError, ValueError, OSError):
        zone = None
    local = datetime.fromtimestamp(now, zone)
    hour = int(match["hour"]) % 12 + (
        12 if match["ampm"].lower() == "pm" else 0
    )
    target = local.replace(
        hour=hour, minute=int(match["minute"] or 0), second=0, microsecond=0
    )
    if match["month"]:
        month = MONTHS.get(match["month"][:3].lower())
        if month is None:
            return None
        target = target.replace(month=month, day=int(match["day"]))
        if target < local - timedelta(days=1):
            target = target.replace(year=target.year + 1)
    elif target <= local:
        target += timedelta(days=1)
    return _clamp(target.timestamp() - now)


def _clamp(seconds: float) -> float | None:
    if seconds > MAX_SECONDS:
        return None
    return max(seconds, 0.0) + MARGIN_SECONDS


def limit_failure(
    text: str, source: str = "runtime", now: float | None = None
) -> Failure | None:
    """Classify provider limit text as a certain, retryable rate limit."""
    if not is_limit(text):
        return None
    wait = seconds_until_reset(text, now)
    detail = " ".join(text.split())[:200]
    return Failure(
        code="provider.rate_limited",
        summary=f"Provider usage limit reached: {detail}",
        source=source,
        uncertain=False,
        retry_after_seconds=FALLBACK_SECONDS if wait is None else wait,
    )


def _entry_text(entry: dict) -> str:
    message = entry.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    parts = []
    if isinstance(content, str):
        parts.append(content)
    elif isinstance(content, list):
        parts.extend(
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
    for key in ("error", "content", "result"):
        if isinstance(entry.get(key), str):
            parts.append(entry[key])
    return "\n".join(parts)


def transcript_failure(
    session_file, now: float | None = None
) -> Failure | None:
    """Find a limit error in the last entries of a Claude transcript.

    Only error entries count, so ordinary text that merely discusses limits
    does not trigger a factory-wide pause.
    """
    if not session_file:
        return None
    try:
        with Path(session_file).open("rb") as stream:
            stream.seek(0, 2)
            stream.seek(max(stream.tell() - TAIL_BYTES, 0))
            lines = stream.read().decode(errors="replace").splitlines()
    except OSError:
        return None
    for line in reversed(lines[-TAIL_ENTRIES:]):
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if not isinstance(entry, dict):
            continue
        if entry.get("isApiErrorMessage") or entry.get("error"):
            failure = limit_failure(_entry_text(entry), "runtime", now)
            if failure:
                return failure
    return None
