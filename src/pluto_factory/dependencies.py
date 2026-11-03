"""Canonical issue references and dependency graph checks without side effects."""

import re

KEY = re.compile(r"^github:([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+):(\d{1,20})$")
REFERENCE = re.compile(r"^(?:([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+))?#(\d{1,20})$")


class DependencyError(ValueError):
    """An invalid, out-of-scope or cyclic dependency declaration."""


def source_key(reference, registered, *, local_slug=None):
    if not isinstance(reference, str):
        raise DependencyError("dependency references must be strings")
    reference = reference.strip()
    match = KEY.fullmatch(reference)
    if match:
        slug, number = match.groups()
    else:
        match = REFERENCE.fullmatch(reference)
        if not match:
            raise DependencyError(
                "use #N, owner/repo#N or a canonical source key"
            )
        slug, number = match.groups()
        slug = slug or local_slug
        if not slug:
            raise DependencyError(
                "local issue dependency needs a source repository"
            )
    slug = slug.lower()
    if slug not in {value.lower() for value in registered}:
        raise DependencyError("dependency repository is outside factory scope")
    if int(number) < 1:
        raise DependencyError("issue dependency numbers must be positive")
    return f"github:{slug}:{int(number)}"


def canonical_existing(value):
    """Read legacy source keys case-insensitively without rewriting the ledger."""
    match = KEY.fullmatch(value or "")
    if match and int(match.group(2)) > 0:
        return f"github:{match.group(1).lower()}:{int(match.group(2))}"
    return value


def graph(items):
    indexed = {item["id"]: item for item in items}
    sources = {}
    ambiguous = set()
    for item in items:
        key = canonical_existing(item.get("source_key"))
        if key:
            if key in sources:
                ambiguous.add(key)
            sources[key] = item["id"]
    edges = {}
    for item in items:
        payload = item["payload"]
        targets = list(payload.get("depends_on", []))
        for reference in payload.get("depends_on_sources", []):
            key = canonical_existing(reference)
            if key in sources and key not in ambiguous:
                targets.append(sources[key])
            else:
                targets.append(f"source:{key}")
        edges[item["id"]] = list(dict.fromkeys(targets))
    return indexed, edges


def has_cycle(edges, start):
    colors = {start: 1}
    stack = [(start, iter(edges.get(start, [])))]
    while stack:
        node, children = stack[-1]
        child = next(children, None)
        if child is None:
            colors[node] = 2
            stack.pop()
        elif colors.get(child) == 1:
            return True
        elif colors.get(child, 0) == 0:
            colors[child] = 1
            stack.append((child, iter(edges.get(child, []))))
    return False


def validate_graph(items, candidate):
    _, edges = graph([*items, candidate])
    if has_cycle(edges, candidate["id"]):
        raise DependencyError("self-dependency or dependency cycle rejected")


def dependency_status(items, work_id):
    indexed, edges = graph(items)
    if work_id not in indexed:
        raise DependencyError("unknown work item")
    missing_sources = set()
    missing_work = set()
    unfinished = set()
    blocked = set()
    seen = {work_id}
    pending = list(edges.get(work_id, []))
    while pending:
        dependency = pending.pop()
        if dependency in seen:
            continue
        seen.add(dependency)
        if dependency.startswith("source:"):
            missing_sources.add(dependency.removeprefix("source:"))
        elif dependency not in indexed:
            missing_work.add(dependency)
        else:
            predecessor = indexed[dependency]
            if predecessor["phase"] != "completed":
                unfinished.add(dependency)
                if predecessor["control"] in {
                    "paused",
                    "deferred",
                    "quarantined",
                }:
                    blocked.add(dependency)
            pending.extend(edges.get(dependency, []))
    cycle = has_cycle(edges, work_id)
    return {
        "ready": not (missing_sources or missing_work or unfinished or cycle),
        "missing_sources": sorted(missing_sources),
        "missing_work": sorted(missing_work),
        "unfinished_work": sorted(unfinished),
        "blocked_work": sorted(blocked),
        "cycle": cycle,
    }
