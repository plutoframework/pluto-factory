"""Deterministic sanitizing of model output; only structure may reject.

Structural invariants (these still raise, in the models): 2-4 options,
exactly one recommended, the required top-level fields present and
non-empty, enum values valid where no safe default exists. Everything
content-level is repaired here and reported, never rejected: word caps,
empty optional text, fix/upstream on the wrong locus, an empty fix on a
framework option, bad enum values (unknown category -> ``other``).
"""

import re

LOCUS = re.compile(r"^(app|framework:[A-Za-z0-9@/_.-]+)$")
REFERENCE = re.compile(r"^(?:[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)?#\d+$")
CATEGORIES = {
    "upstream_issue",
    "resume_after_fix",
    "app_fix",
    "config_change",
    "security",
    "credentials_spend",
    "acceptance_scope",
    "public_release",
    "product_ux",
    "other",
}
CAPS = {
    "problem": 40,
    "detail": 15,
    "why": 25,
    "label": 8,
    "effect": 25,
    "tradeoff": 25,
    "fix": 25,
    "autonomy_reason": 25,
}
MAX_CHANGES = 20


class Changes(list):
    """Bounded, de-duplicated list of what sanitizing changed."""

    def add(self, message):
        if message not in self and len(self) < MAX_CHANGES:
            self.append(message)


def _text(value):
    return " ".join(str(value).split()) if isinstance(value, (str, int)) else ""


def _words(value, limit, where, changes):
    text = _text(value)
    pieces = text.split()
    if len(pieces) > limit:
        changes.add(f"{where}: clipped to {limit} words")
        text = " ".join(pieces[:limit])
    # A "word" can be thousands of characters long.
    ceiling = limit * 20
    if len(text) > ceiling:
        changes.add(f"{where}: clipped to {ceiling} characters")
        text = text[:ceiling].rstrip()
    return text


def _chars(value, limit, where, changes, multiline=False):
    text = str(value).strip() if isinstance(value, str) else ""
    if not multiline:
        text = " ".join(text.split())
    if len(text) > limit:
        changes.add(f"{where}: clipped to {limit} characters")
        text = text[:limit].rstrip()
    return text


def sanitize_upstream(raw, option, changes, where):
    """A usable payload, or None (the caller downgrades the locus)."""
    if not isinstance(raw, dict):
        return None
    effect = option.get("effect", "")
    fix = _chars(raw.get("fix"), 2000, f"{where}.upstream.fix", changes)
    fix = fix or option.get("fix", "") or effect
    problem = _chars(
        raw.get("problem"), 2000, f"{where}.upstream.problem", changes
    )
    problem = problem or effect
    title = _chars(raw.get("title"), 80, f"{where}.upstream.title", changes)
    title = title or _chars(fix, 80, f"{where}.upstream.title", changes)
    repro = _chars(raw.get("repro"), 2000, f"{where}.upstream.repro", changes)
    if not repro:
        repro = "See the problem description."
        changes.add(f"{where}.upstream.repro: filled")
    items = raw.get("acceptance")
    acceptance = []
    for item in items if isinstance(items, list) else []:
        text = _chars(item, 300, f"{where}.upstream.acceptance", changes)
        if text:
            acceptance.append(text)
    if len(acceptance) > 8:
        acceptance = acceptance[:8]
        changes.add(f"{where}.upstream.acceptance: truncated to 8")
    if not acceptance and fix:
        acceptance = [_chars(fix, 300, where, changes)]
        changes.add(f"{where}.upstream.acceptance: filled from fix")
    if not (title and problem and fix and acceptance):
        return None
    return {
        "title": title,
        "problem": problem,
        "repro": repro,
        "fix": fix,
        "acceptance": acceptance,
    }


def _option(raw, index, changes, *, pull_request):
    where = f"options.{index}"
    if not isinstance(raw, dict):
        return raw
    option = dict(raw)
    option["effect"] = _words(
        option.get("effect"), CAPS["effect"], f"{where}.effect", changes
    )
    option["tradeoff"] = _words(
        option.get("tradeoff"), CAPS["tradeoff"], f"{where}.tradeoff", changes
    )
    option["label"] = _words(
        option.get("label"), CAPS["label"], f"{where}.label", changes
    )
    if not option["label"]:
        option["label"] = (
            _words(option["effect"], CAPS["label"], f"{where}.label", changes)
            or f"Option {index + 1}"
        )
        changes.add(f"{where}.label: filled")
    if not option["effect"]:
        option["effect"] = option["label"]
        changes.add(f"{where}.effect: filled from label")
    if not option["tradeoff"]:
        option["tradeoff"] = "None stated."
        changes.add(f"{where}.tradeoff: filled")
    option["recommended"] = bool(option.get("recommended"))
    option["completes"] = bool(option.get("completes"))
    locus = _text(option.get("locus")) or "app"
    if not LOCUS.fullmatch(locus):
        changes.add(f"{where}.locus: invalid, set to app")
        locus = "app"
    if pull_request and locus != "app":
        changes.add(f"{where}.locus: pull-request items are app only")
        locus = "app"
    fix = _words(option.get("fix"), CAPS["fix"], f"{where}.fix", changes)
    upstream = option.get("upstream")
    if locus.startswith("framework:"):
        if not fix:
            fix = _words(option["effect"], CAPS["fix"], where, changes)
            changes.add(f"{where}.fix: filled from effect")
        payload = sanitize_upstream(
            upstream, {**option, "fix": fix}, changes, where
        )
        if payload is None:
            changes.add(
                f"{where}: framework option without a usable upstream "
                "payload downgraded to app"
            )
            locus, fix, payload = "app", "", None
        option["upstream"] = payload
    else:
        if fix or upstream:
            changes.add(f"{where}: dropped fix/upstream on an app option")
        fix, option["upstream"] = "", None
    option["locus"], option["fix"] = locus, fix
    return option


def sanitize_advice(raw, *, pull_request=False):
    """(repaired dict, changes). Non-dicts and structure pass through."""
    changes = Changes()
    if not isinstance(raw, dict):
        return raw, changes
    advice = dict(raw)
    advice["problem"] = _words(
        advice.get("problem"), CAPS["problem"], "problem", changes
    )
    details = advice.get("details")
    clean = []
    for item in details if isinstance(details, list) else []:
        text = _words(item, CAPS["detail"], "details", changes)
        if text:
            clean.append(text)
    if len(clean) > 6:
        clean = clean[:6]
        changes.add("details: truncated to 6")
    advice["details"] = clean
    options = advice.get("options")
    if isinstance(options, list):
        advice["options"] = [
            _option(o, i, changes, pull_request=pull_request)
            for i, o in enumerate(options)
        ]
        seen = {}
        for option in advice["options"]:
            if isinstance(option, dict):
                label = option["label"]
                seen[label] = seen.get(label, 0) + 1
                if seen[label] > 1:
                    option["label"] = f"{label} ({seen[label]})"
                    changes.add("options: duplicate label renamed")
    why = _words(advice.get("why"), CAPS["why"], "why", changes)
    if not why:
        why = "Recommended option is the safest first step."
        changes.add("why: filled")
    advice["why"] = why
    category = _text(advice.get("category")) or "other"
    if category not in CATEGORIES:
        changes.add(f"category: {category!r} unknown, set to other")
        category = "other"
    advice["category"] = category
    for field, allowed, default in (
        ("autonomy", {"auto", "owner"}, "owner"),
        ("confidence", {"high", "medium", "low"}, "low"),
    ):
        value = _text(advice.get(field)) or default
        if value not in allowed:
            changes.add(f"{field}: {value!r} invalid, set to {default}")
            value = default
        advice[field] = value
    advice["reversible"] = advice.get("reversible") is True
    reason = _words(
        advice.get("autonomy_reason"),
        CAPS["autonomy_reason"],
        "autonomy_reason",
        changes,
    )
    if advice["autonomy"] == "auto" and not reason:
        changes.add("autonomy: auto without a reason, set to owner")
        advice["autonomy"] = "owner"
    advice["autonomy_reason"] = reason
    change = advice.get("config_change")
    valid = (
        isinstance(change, dict)
        and isinstance(change.get("key"), str)
        and isinstance(change.get("value"), (int, float))
        and not isinstance(change.get("value"), bool)
    )
    if (category == "config_change") != valid:
        if category == "config_change":
            changes.add(
                "category: config_change without a change, set to other"
            )
            advice["category"] = "other"
        else:
            changes.add(
                "config_change: dropped (category is not config_change)"
            )
        advice["config_change"] = None
    elif not valid:
        advice["config_change"] = None
    return advice, changes


# -- triage ------------------------------------------------------------------


def sanitize_spec(raw, changes):
    if not isinstance(raw, dict):
        return raw
    spec = dict(raw)
    title = str(spec.get("title") or "").strip().splitlines()
    spec["title"] = _words(title[0] if title else "", 15, "spec.title", changes)
    spec["scope"] = _words(spec.get("scope"), 150, "spec.scope", changes)
    items = spec.get("acceptance")
    acceptance = []
    for item in items if isinstance(items, list) else []:
        text = _words(item, 40, "spec.acceptance", changes)
        if text:
            acceptance.append(text)
    if len(acceptance) > 10:
        acceptance = acceptance[:10]
        changes.add("spec.acceptance: truncated to 10")
    spec["acceptance"] = acceptance
    depends = []
    for item in (
        spec.get("depends") if isinstance(spec.get("depends"), list) else []
    ):
        text = _text(item)
        if REFERENCE.fullmatch(text):
            depends.append(text)
        else:
            changes.add("spec.depends: invalid reference dropped")
    spec["depends"] = depends[:10]
    locus = _text(spec.get("locus")) or "app"
    if not LOCUS.fullmatch(locus):
        changes.add("spec.locus: invalid, set to app")
        locus = "app"
    spec["locus"] = locus
    return spec


def sanitize_triage(raw):
    """(repaired dict, changes) for a TriageResult reply."""
    changes = Changes()
    if not isinstance(raw, dict):
        return raw, changes
    result = dict(raw)
    if isinstance(result.get("advice"), dict):
        result["advice"], inner = sanitize_advice(result["advice"])
        for message in inner:
            changes.add(f"advice.{message}")
    if isinstance(result.get("spec"), dict):
        result["spec"] = sanitize_spec(result["spec"], changes)
    rejection = result.get("rejection")
    if isinstance(rejection, dict):
        rejection = dict(rejection)
        if rejection.get("category") not in {
            "duplicate",
            "invalid",
            "out_of_scope",
        }:
            changes.add("rejection.category: unknown, set to invalid")
            rejection["category"] = "invalid"
        rejection["reason"] = _words(
            rejection.get("reason"), 40, "rejection.reason", changes
        )
        result["rejection"] = rejection
    return result, changes
