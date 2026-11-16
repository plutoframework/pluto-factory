"""``decisions`` and ``prs`` subcommands over the factory ledger."""

import getpass
import re

from . import attention

TARGET = re.compile(r"^([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)#([1-9][0-9]*)$")


def _options(args):
    options = []
    for raw in args.option:
        label, _, description = raw.partition("=")
        options.append(
            {
                "label": label.strip(),
                "description": description.strip(),
                "recommended": label.strip() == args.recommended,
            }
        )
    if args.recommended and not any(o["recommended"] for o in options):
        raise ValueError("--recommended must name one of the --option labels")
    return options


def decisions(state, args):
    operation = args.operation or "list"
    if operation == "list":
        wanted = set(args.state) or {"open", "answered"}
        return {"decisions": attention.list_decisions(state, wanted)}
    if operation == "add":
        if not args.question:
            raise ValueError("decisions add needs --question")
        kind = args.kind or ("choice" if len(args.option) > 1 else "free_text")
        proposal = None
        if args.config_key:
            kind = "config"
            if args.config_value is None:
                raise ValueError("--config-key needs --config-value")
            try:
                value = float(args.config_value)
            except ValueError as error:
                raise ValueError("--config-value must be a number") from error
            proposal = {
                "key": args.config_key,
                "value": int(value) if value == int(value) else value,
            }
        return attention.add_decision(
            state,
            question=args.question,
            context=args.context,
            options=_options(args),
            kind=kind,
            work_id=args.work,
            proposal=proposal,
        )
    if not args.id:
        raise ValueError(f"decisions {operation} needs a decision id")
    if operation == "show":
        return attention.get_decision(state, args.id)
    if operation == "withdraw":
        return attention.withdraw_decision(state, args.id, args.note)
    if not args.answer:
        raise ValueError("decisions answer needs --answer")
    result = attention.answer_decision(
        state, args.id, args.answer, args.note, via=f"cli:{getpass.getuser()}"
    )
    result["next"] = "The coordinator applies the answer on its next tick."
    return result


def pull_requests(state, args):
    operation = args.operation or "list"
    if operation == "list":
        rows = attention.list_prs(state, set(args.state) or None)
        return {
            "pull_requests": [
                {k: v for k, v in row.items() if k != "log_excerpt"}
                for row in rows
                if row["state"] != "untracked" or args.state
            ]
        }
    match = TARGET.fullmatch(args.target or "")
    if not match:
        raise ValueError("expected a target like owner/name#123")
    slug, number = match[1], int(match[2])
    if operation == "untrack":
        return attention.untrack(state, slug, number)
    row = attention.get_pr(state, slug, number)
    if row and row["state"] == "untracked":
        attention.set_pr(state, row, state="open", reason="")
    return attention.track_pr(state, slug, number, origin="manual")


def run(state, args):
    if args.command == "decisions":
        return decisions(state, args)
    return pull_requests(state, args)
