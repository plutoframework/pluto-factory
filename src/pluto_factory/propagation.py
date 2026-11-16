"""Framework fix propagation over GitHub, in two halves.

Receiving factory (owns the library): when work that came from a triaged
issue in a library repo completes, queue follow-up work so the fix becomes
consumable (sibling modules pinned to the merge commit, then a CLI pin
bump), announce the final item on the upstream issue and later mark it done.
Dependent factory: an upstream issue only unblocks work once the fix is
reachable from the library revision the newest CLI revision pins.
"""

import base64
import json
import re

from .github import GitHubFailure
from .models import Submission
from .state import encode

FINAL = "<!-- pluto-factory:propagation-final:{key} -->"
DONE = "<!-- pluto-factory:propagation-done:{key} -->"
FINAL_RE = re.compile(r"<!-- pluto-factory:propagation-final:(\S+) -->")
SOURCE = re.compile(r"^github:([^:]+):(\d+)$")


def _comments(adapter, slug, number):
    view = adapter._json(
        [
            "gh",
            "issue",
            "view",
            str(number),
            "--repo",
            slug,
            "--json",
            "comments",
        ]
    )
    return [c.get("body") or "" for c in view.get("comments") or []]


def _comment_once(adapter, slug, number, marker, text):
    if any(marker in body for body in _comments(adapter, slug, number)):
        return
    adapter._run(
        [
            "gh",
            "issue",
            "comment",
            str(number),
            "--repo",
            slug,
            "--body",
            f"{marker}\n{text}",
        ],
        mutating=True,
    )


def _save(state, work_id, work, prop):
    data = {**work["data"], "propagation": prop}
    with state.db:
        state.db.execute(
            "UPDATE work SET data=? WHERE id=?", (encode(data), work_id)
        )


def _merge_commit(work, repo_name):
    for pr in work["data"].get("pull_requests", []):
        if pr.get("repository") == repo_name and pr.get("merge_commit"):
            return pr["merge_commit"]
    return ""


def _plan(config, work, repo, rule, commit):
    """[(step key, title, instruction, repositories)] in dependency order."""
    where = f"merge commit {commit}" if commit else "the merged revision"
    steps = []
    for module in rule.sibling_modules:
        steps.append(
            (
                f"module:{module}",
                f"Pin {module} to the {repo.name} fix",
                (
                    f"{module} in {repo.name} needs the new {repo.name} API "
                    f"from a published version. Require the root module "
                    f"pinned to {where}, adapt {module} to it, and keep its "
                    "checks green."
                ),
                [repo.name],
            )
        )
    if rule.cli_bump:
        steps.append(
            (
                "cli",
                f"Bump CLI pin of {repo.name}",
                (
                    f"Bump the CLI's pinned revision of {repo.name} to "
                    f"{where} (including sibling modules landed above) and "
                    "regenerate goldens."
                ),
                [config.propagation.cli_repo],
            )
        )
    return steps


def propagate(state, adapter, config):
    """Create follow-ups, announce the final item, mark it done."""
    policy = config.propagation
    if not policy.cli_repo:
        return 0
    changed = 0
    repos = {r.github.lower(): r for r in config.repositories if r.github}
    for item in state.list_work():
        if item["phase"] != "completed":
            continue
        work = state.get_work(item["id"])
        prop = work["data"].get("propagation")
        if prop and prop.get("done_posted"):
            continue
        match = SOURCE.fullmatch(work.get("source_key") or "")
        if not match:
            continue
        slug, number = match[1], int(match[2])
        repo = repos.get(slug.lower())
        rule = policy.rule(repo.name) if repo else None
        if repo is None or repo.name == policy.cli_repo or rule is None:
            continue
        triaged = state.db.execute(
            "SELECT 1 FROM triage_issues WHERE lower(slug)=? AND number=?",
            (slug.lower(), number),
        ).fetchone()
        if not triaged:
            continue
        try:
            if prop is None:
                commit = _merge_commit(work, repo.name)
                if work["data"].get("pull_requests") and not commit:
                    continue  # merge commit not recorded yet
                previous, ids, key = work["id"], [], ""
                for step, title, instruction, repositories in _plan(
                    config, work, repo, rule, commit
                ):
                    made = state.submit(
                        Submission(
                            title=title,
                            instruction=instruction,
                            acceptance=[
                                "Checks pass",
                                "The fix is consumable through the pin",
                            ],
                            repositories=repositories,
                            source="propagation",
                            delivery_key=f"{work['id']}:{step}",
                            depends_on=[previous],
                        )
                    )
                    previous = made["id"]
                    ids.append(made["id"])
                    key = f"propagation:{work['id']}:{step}"
                prop = {
                    "items": ids,
                    "final_key": key,
                    "announced": False,
                    "done_posted": not ids,
                }
                _save(state, work["id"], work, prop)
                changed += 1
            if prop["final_key"] and not prop["announced"]:
                _comment_once(
                    adapter,
                    slug,
                    number,
                    FINAL.format(key=prop["final_key"]),
                    "Follow-up work queued so this fix is consumable "
                    "downstream.",
                )
                prop["announced"] = True
                _save(state, work["id"], work, prop)
                changed += 1
            if prop["announced"] and not prop["done_posted"]:
                last = state.get_work(prop["items"][-1])
                if last["phase"] == "completed":
                    _comment_once(
                        adapter,
                        slug,
                        number,
                        DONE.format(key=prop["final_key"]),
                        "Follow-up work is complete.",
                    )
                    prop["done_posted"] = True
                    _save(state, work["id"], work, prop)
                    changed += 1
        except GitHubFailure as error:
            state.event(
                "propagation.failed",
                work["id"],
                {"code": error.code},
                f"propagation-failed:{work['id']}:{error.code}",
            )
    return changed


# -- dependent side -----------------------------------------------------------


def _gh_api(adapter, path):
    return adapter._json(["gh", "api", path])


def _pin_commitish(raw):
    pseudo = re.fullmatch(r"v\d[\w.]*-(?:\d+\.)?\d{14}-([0-9a-f]{12})", raw)
    return pseudo[1] if pseudo else raw


def fix_status(adapter, config, slug, number, view):
    """(ready, pending reason, cli revision) for a closed-as-completed issue."""
    policy = config.framework
    if not policy.cli_repo:
        return True, "", ""
    try:
        comments = _comments(adapter, slug, number)
        finals = [
            m[1] for body in comments for m in [FINAL_RE.search(body)] if m
        ]
        if finals:
            done = DONE.format(key=finals[-1])
            if not any(done in body for body in comments):
                return False, "propagation follow-up work is not done", ""
        library = slug.rsplit("/", 1)[-1]
        pin = policy.cli_pins.get(library) or next(
            (
                v
                for k, v in policy.cli_pins.items()
                if k.lower() == library.lower()
            ),
            None,
        )
        if pin is None:
            return False, f"no CLI pin source configured for {library}", ""
        references = view.get("closedByPullRequestsReferences") or []
        ref = next(
            (
                re.search(r"github\.com/([^/]+/[^/]+)/pull/(\d+)", r["url"])
                for r in references
                if r.get("url")
            ),
            None,
        )
        if not ref:
            return False, "closing PR unknown", ""
        pr = adapter._json(
            [
                "gh",
                "pr",
                "view",
                ref[2],
                "--repo",
                ref[1],
                "--json",
                "mergeCommit",
            ]
        )
        fix = (pr.get("mergeCommit") or {}).get("oid")
        if not fix:
            return False, "closing PR has no merge commit", ""
        head = _gh_api(
            adapter, f"repos/{policy.cli_repo}/commits/{policy.cli_ref}"
        ).get("sha")
        if not head:
            return False, "CLI revision unreadable", ""
        contents = _gh_api(
            adapter,
            f"repos/{policy.cli_repo}/contents/{pin.path}?ref={head}",
        )
        text = base64.b64decode(contents.get("content") or "").decode(
            errors="replace"
        )
        found = re.search(pin.pattern, text)
        if not found:
            return False, f"CLI pin of {library} not found", head
        pinned = _pin_commitish(found[1])
        compare = _gh_api(adapter, f"repos/{slug}/compare/{fix}...{pinned}")
        if compare.get("status") in {"identical", "ahead"}:
            return True, "", head
        return (
            False,
            (
                f"fix {fix[:12]} not yet in the CLI's pinned {library} {pinned[:12]}"
            ),
            head,
        )
    except (GitHubFailure, ValueError, json.JSONDecodeError) as error:
        return False, f"unverifiable ({getattr(error, 'code', 'error')})", ""
