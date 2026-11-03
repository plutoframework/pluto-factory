"""CLI presentation over the Python factory's typed operations."""

import argparse
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from . import __version__
from .cli_github import mappings, origin_slug, readiness, resolve_repositories
from .config import factory_root, load_config, write_config
from .credentials import (
    CredentialError,
    scaffold_secret_config,
    secret_readiness,
    set_reference,
    set_secret,
)
from .defaults import SecretDefaults, merge, read_defaults
from .models import (
    FactoryConfig,
    ImprovementPolicy,
    ModelProfile,
    Repository,
    Submission,
)


def _common(parser: argparse.ArgumentParser, *, root: bool = False) -> None:
    default = None if root else argparse.SUPPRESS
    parser.add_argument("--home", type=Path, default=default)
    parser.add_argument("--factory", default=default)
    parser.add_argument(
        "--no-input", action="store_true", default=False if root else default
    )
    parser.add_argument(
        "--interactive", action="store_true", default=False if root else default
    )
    parser.add_argument("--json", action="store_true", default=default)


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        prog="pluto-factory",
        description="Run the local Pluto software factory prototype.",
    )
    root.add_argument("--version", action="version", version=__version__)
    _common(root, root=True)
    commands = root.add_subparsers(dest="command", required=True)

    inventory = commands.add_parser(
        "list", help="List registered local factories"
    )
    _common(inventory)
    inventory.add_argument(
        "--root", type=Path, help="Alternate factory registry directory"
    )

    setup = commands.add_parser(
        "setup", aliases=["init"], help="Create a factory configuration"
    )
    _common(setup)
    setup.add_argument("--name")
    setup.add_argument(
        "--repo", action="append", default=[], metavar="NAME=PATH"
    )
    setup.add_argument("--read-repo", action="append", default=[])
    setup.add_argument(
        "--check", action="append", default=[], metavar="ARGV_JSON"
    )
    setup.add_argument("--qa", action="append", default=[], metavar="ARGV_JSON")
    setup.add_argument("--config", type=Path)
    setup.add_argument("--defaults-file", type=Path)
    setup.add_argument("--harness", choices=["pi", "fixture"], default="pi")
    setup.add_argument(
        "--coordinator", choices=["model", "rules"], default="model"
    )
    setup.add_argument(
        "--isolation", choices=["required", "cooperative"], default="required"
    )
    setup.add_argument("--herdr-space", default="")
    setup.add_argument(
        "--trust-repositories",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Trust project guidance in registered repositories for each Pi run",
    )
    setup.add_argument(
        "--integration", choices=["managed", "github"], default="managed"
    )
    setup.add_argument(
        "--github-repo", action="append", default=[], metavar="NAME=OWNER/REPO"
    )
    setup.add_argument("--issue-label", default="factory:ready")
    setup.add_argument("--poll-seconds", type=int, default=30)
    setup.add_argument(
        "--auto-merge", action=argparse.BooleanOptionalAction, default=False
    )
    setup.add_argument(
        "--close-source-issues",
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    setup.add_argument("--model", default="gpt-6.1-sol")
    setup.add_argument("--provider")
    setup.add_argument("--reasoning", default="high")
    setup.add_argument("--coordinator-model")
    setup.add_argument("--review-model", default="gpt-6.1-sol")
    setup.add_argument("--qa-model", default="gpt-6.1-terra")
    setup.add_argument(
        "--auth", choices=["existing", "codex", "api_key"], default="existing"
    )
    setup.add_argument("--api-key-env", default="OPENAI_API_KEY")
    setup.add_argument("--api-key-stdin", action="store_true")
    setup.add_argument(
        "--secrets-provider", choices=["age", "1password"], default="age"
    )
    setup.add_argument("--age-recipient")
    setup.add_argument("--age-key-file", type=Path)
    setup.add_argument(
        "--generate-age-key",
        action=argparse.BooleanOptionalAction,
        default=False,
    )
    setup.add_argument("--op-vault")
    setup.add_argument("--op-account")
    setup.add_argument("--op-item")
    setup.add_argument("--slack-webhook-env", default="")
    setup.add_argument(
        "--self-improvement",
        choices=["disabled", "propose", "automatic"],
        default="propose",
    )
    setup.add_argument("--improvement-area", action="append", default=[])
    setup.add_argument("--max-recoveries", type=int, default=3)
    setup.add_argument("--max-revisions", type=int, default=5)
    setup.add_argument("--phase-timeout", type=int, default=900)
    setup.add_argument("--command-timeout", type=int, default=120)
    setup.add_argument("--retry-base", type=float, default=5)
    setup.add_argument(
        "--flag-env", action="append", default=[], metavar="JSON"
    )
    setup.add_argument("--yes", action="store_true")

    submit = commands.add_parser("submit", help="Durably submit a work item")
    _common(submit)
    submit.add_argument("--file", type=Path)
    submit.add_argument("--title")
    submit.add_argument("--instruction")
    submit.add_argument("--acceptance", action="append", default=[])
    submit.add_argument("--repository", action="append", default=[])
    submit.add_argument("--source", default="cli")
    submit.add_argument("--delivery-key")
    submit.add_argument("--depends-on", action="append", default=[])

    for name in ("start", "run"):
        start = commands.add_parser(name, help="Run the durable coordinator")
        _common(start)
        start.add_argument("--once", action="store_true")
        start.add_argument("--max-seconds", type=float)
        start.add_argument(
            "--watch", action=argparse.BooleanOptionalAction, default=None
        )

    for name in ("inspect", "doctor", "pause", "resume", "notifications"):
        command = commands.add_parser(name)
        _common(command)
        if name in {"pause", "resume"}:
            command.add_argument("--work", help="Limit the action to one item")
        if name == "doctor":
            command.add_argument("--verify-auth", action="store_true")
        if name == "notifications":
            command.add_argument(
                "--deliver",
                action="store_true",
                help="Deliver configured outbound notifications",
            )

    auth = commands.add_parser("auth", help="Run a provider's native login")
    _common(auth)
    auth.add_argument("provider", choices=["codex", "api_key"])
    auth.add_argument("--api-key-env", default="OPENAI_API_KEY")
    auth.add_argument("--api-key-stdin", action="store_true")
    auth.add_argument("--op-item")

    defaults = commands.add_parser(
        "defaults", help="Inspect master factory defaults"
    )
    _common(defaults)
    defaults.add_argument("operation", choices=["show"])
    defaults.add_argument("--defaults-file", type=Path)

    improvements = commands.add_parser(
        "improvements",
        help="Inspect or activate owner-permitted prompt changes",
    )
    _common(improvements)
    improvements.add_argument(
        "operation", choices=["list", "activate", "rollback"]
    )
    improvements.add_argument("id", nargs="?")

    demo = commands.add_parser("demo", help="Run disposable fixture examples")
    _common(demo)
    demo.add_argument("--directory", type=Path, required=True)
    demo.add_argument(
        "--scenario",
        default="fix",
        choices=[
            "fix",
            "crash_once",
            "malformed_once",
            "provider_once",
            "idle_no_report_once",
        ],
    )
    demo.add_argument("--max-seconds", type=float, default=60)
    return root


def _home(args: argparse.Namespace, name: str | None = None) -> Path:
    if args.home is not None:
        return args.home.expanduser().resolve()
    configured = os.environ.get("PLUTO_FACTORY_HOME")
    if configured:
        return Path(configured).expanduser().resolve()
    return (factory_root() / (name or args.factory or "default")).resolve()


def _argv(value: str) -> list[str]:
    parsed = json.loads(value)
    if not isinstance(parsed, list) or not parsed:
        raise ValueError("a check must be a nonempty JSON argument array")
    if not all(isinstance(part, str) and part for part in parsed):
        raise ValueError("check arguments must be nonempty strings")
    return parsed


def _repository(
    value: str, access: str, args: argparse.Namespace
) -> Repository:
    name, separator, path = value.partition("=")
    if not separator or not name or not path:
        raise ValueError("repositories use NAME=PATH")
    resolved = Path(path).expanduser().resolve(strict=True)
    result = subprocess.run(
        ["git", "-C", str(resolved), "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        raise ValueError(f"{name}: path is not a Git repository")
    if Path(result.stdout.strip()).resolve() != resolved:
        raise ValueError(f"{name}: register the repository root")
    return Repository(
        name=name,
        path=str(resolved),
        access=access,
        checks=[_argv(value) for value in args.check],
        qa=[_argv(value) for value in args.qa],
        flag_environments=[json.loads(value) for value in args.flag_env]
        or [{}],
    )


def _apply_master(args: argparse.Namespace) -> dict:
    defaults = read_defaults(args.defaults_file)
    values = defaults["factory"]
    mapping = {
        "harness": "harness",
        "coordinator": "coordinator",
        "isolation": "isolation",
        "trust_repositories": "trust_registered_repositories",
        "auth": "auth_mode",
        "api_key_env": "api_key_env",
        "herdr_space": "herdr_space",
        "slack_webhook_env": "slack_webhook_env",
        "coordinator_model": "coordinator_model",
        "max_recoveries": "max_recoveries",
        "max_revisions": "max_revisions",
        "phase_timeout": "phase_timeout_seconds",
        "command_timeout": "command_timeout_seconds",
        "retry_base": "retry_base_seconds",
        "integration": "integration",
        "issue_label": "github_issue_label",
        "poll_seconds": "github_poll_seconds",
        "auto_merge": "github_auto_merge",
        "close_source_issues": "close_source_issues",
    }
    explicit = getattr(args, "_explicit", set())
    for argument, field in mapping.items():
        flag = "--" + argument.replace("_", "-")
        if flag not in explicit and "--no-" + flag[2:] not in explicit:
            setattr(args, argument, values[field])
    for argument, value in {
        "model": values["worker"]["model"],
        "provider": values["worker"]["provider"],
        "reasoning": values["worker"]["reasoning"],
        "review_model": values["reviewer"]["model"],
        "qa_model": values["qa_model"]["model"],
        "self_improvement": values["self_improvement"]["mode"],
        "improvement_area": values["self_improvement"]["allowed_areas"],
    }.items():
        flag = "--" + argument.replace("_", "-")
        if flag not in explicit:
            setattr(args, argument, value)
    for argument, value in defaults["secrets"].items():
        flag = (
            "--secrets-provider"
            if argument == "provider"
            else ("--" + argument.replace("_", "-"))
        )
        if flag not in explicit and "--no-" + flag[2:] not in explicit:
            target = "secrets_provider" if argument == "provider" else argument
            setattr(
                args,
                target,
                Path(value) if argument == "age_key_file" else value,
            )
    if args.auth == "api_key":
        if "--provider" not in explicit and args.provider == "openai-codex":
            args.provider = "openai"
        if (
            "--coordinator-model" not in explicit
            and args.coordinator_model.startswith("openai-codex:")
        ):
            args.coordinator_model = args.coordinator_model.replace(
                "openai-codex:", "openai:", 1
            )
    return defaults


def _ask(label: str, default: str) -> str:
    return input(f"{label} ({default}): ").strip() or default


def _wizard(args: argparse.Namespace) -> bool:
    if args.interactive and args.no_input:
        raise ValueError("--interactive and --no-input cannot be combined")
    if args.no_input:
        return False
    if not args.interactive and (
        args.name or args.repo or args.read_repo or args.config
    ):
        return False
    ci = os.environ.get("CI", "").lower() not in {"", "0", "false"}
    if ci or not sys.stdin.isatty() or not sys.stdout.isatty():
        raise ValueError("setup needs --name and --repo in noninteractive mode")
    if args.config:
        return True
    from .setup_inputs import factory_name, repository_choice

    args.name = factory_name(_ask("Factory name", args.name or ""))
    while True:
        first = not args.repo and not args.read_repo
        prompt = (
            f"Repository path or NAME=PATH (blank uses {Path.cwd()}): "
            if first
            else "Additional repository NAME=PATH (blank to finish): "
        )
        repository = input(prompt).strip()
        if not repository:
            if not first:
                break
            args.repo.append(repository_choice(""))
            continue
        args.repo.append(repository_choice(repository))
    if not args.check:
        args.check = [
            input('Check argv JSON (for example ["python3","-m","unittest"]): ')
        ]
    if not args.qa:
        args.qa = [input("QA argv JSON: ")]
    args.harness = _ask("Worker harness [pi/fixture]", args.harness)
    args.integration = _ask("Integration [managed/github]", args.integration)
    if args.integration == "github":
        registered = mappings(args.github_repo)
        for value in args.repo:
            name, separator, path = value.partition("=")
            if not separator:
                raise ValueError("repositories use NAME=PATH")
            suggested_slug = registered.get(name) or origin_slug(
                Path(path).expanduser()
            )
            registered[name] = _ask(
                f"GitHub OWNER/REPOSITORY for {name}", suggested_slug
            )
        args.github_repo = [
            f"{name}={slug}" for name, slug in registered.items()
        ]
        args.issue_label = _ask("GitHub ready label", args.issue_label)
        args.poll_seconds = int(
            _ask("GitHub poll interval in seconds", str(args.poll_seconds))
        )
        args.auto_merge = (
            _ask(
                "Automatically merge validated PRs [yes/no]",
                "yes" if args.auto_merge else "no",
            ).lower()
            == "yes"
        )
        args.close_source_issues = (
            _ask(
                "Close source issues after completed integration [yes/no]",
                "yes" if args.close_source_issues else "no",
            ).lower()
            == "yes"
        )
    args.isolation = _ask("Isolation [required/cooperative]", args.isolation)
    args.trust_repositories = (
        _ask(
            "Trust registered repository guidance for unattended Pi runs [yes/no]",
            "yes" if args.trust_repositories else "no",
        ).lower()
        == "yes"
    )
    args.auth = _ask("Authentication [existing/codex/api_key]", args.auth)
    suggested = "openai" if args.auth == "api_key" else args.provider
    args.provider = _ask("Worker provider", suggested)
    args.model = _ask("Implementation model", args.model)
    args.reasoning = _ask("Worker reasoning", args.reasoning)
    args.review_model = _ask("Review model", args.review_model)
    args.qa_model = _ask("QA model", args.qa_model)
    args.coordinator = _ask("Coordinator [model/rules]", args.coordinator)
    args.coordinator_model = _ask("Coordinator model", args.coordinator_model)
    args.secrets_provider = _ask(
        "fnox backend [age/1password]", args.secrets_provider
    )
    if args.secrets_provider == "age":
        key_path = _ask(
            "Existing private age identity path; blank generates one",
            str(args.age_key_file or ""),
        )
        if key_path:
            args.age_key_file = Path(key_path)
            args.generate_age_key = False
        else:
            args.generate_age_key = True
            args.age_recipient = None
    else:
        args.op_vault = _ask("1Password vault", args.op_vault or "") or None
        args.op_account = (
            _ask("1Password account", args.op_account or "") or None
        )
        if args.auth == "api_key":
            args.op_item = input(
                "API key op://vault/item/field reference: "
            ).strip()
    if args.auth == "api_key":
        args.api_key_env = _ask(
            "API key environment reference", args.api_key_env
        )
    args.herdr_space = _ask(
        "Herdr space ID; blank for headless", args.herdr_space
    )
    args.slack_webhook_env = _ask(
        "Slack webhook environment reference", args.slack_webhook_env
    )
    args.self_improvement = _ask(
        "Self-improvement [disabled/propose/automatic]", args.self_improvement
    )
    areas = _ask(
        "Improvement areas, comma separated", ",".join(args.improvement_area)
    )
    args.improvement_area = [
        part.strip() for part in areas.split(",") if part.strip()
    ]
    return True


def _setup(args: argparse.Namespace) -> dict:
    master = _apply_master(args)
    interactive = _wizard(args)
    if args.config:
        import tomllib

        with args.config.open("rb") as stream:
            configured = merge(master["factory"], tomllib.load(stream))
        explicit = getattr(args, "_explicit", set())
        for argument, field in {
            "name": "name",
            "harness": "harness",
            "coordinator": "coordinator",
            "isolation": "isolation",
            "trust_repositories": "trust_registered_repositories",
            "auth": "auth_mode",
            "api_key_env": "api_key_env",
            "herdr_space": "herdr_space",
            "slack_webhook_env": "slack_webhook_env",
            "coordinator_model": "coordinator_model",
            "max_recoveries": "max_recoveries",
            "max_revisions": "max_revisions",
            "phase_timeout": "phase_timeout_seconds",
            "command_timeout": "command_timeout_seconds",
            "retry_base": "retry_base_seconds",
            "integration": "integration",
            "issue_label": "github_issue_label",
            "poll_seconds": "github_poll_seconds",
            "auto_merge": "github_auto_merge",
            "close_source_issues": "close_source_issues",
        }.items():
            flag = "--" + argument.replace("_", "-")
            if flag in explicit or "--no-" + flag[2:] in explicit:
                configured[field] = getattr(args, argument)
        for argument, role, field in (
            ("model", "worker", "model"),
            ("provider", "worker", "provider"),
            ("reasoning", "worker", "reasoning"),
            ("review_model", "reviewer", "model"),
            ("qa_model", "qa_model", "model"),
        ):
            if "--" + argument.replace("_", "-") in explicit:
                configured[role][field] = getattr(args, argument)
        if "--repo" in explicit or "--read-repo" in explicit:
            configured["repositories"] = [
                _repository(value, "write", args).model_dump()
                for value in args.repo
            ] + [
                _repository(value, "read", args).model_dump()
                for value in args.read_repo
            ]
        config = FactoryConfig.model_validate(configured)
    else:
        if not args.name or not (args.repo or args.read_repo):
            raise ValueError("setup requires --name and at least one --repo")
        repositories = [
            _repository(value, "write", args) for value in args.repo
        ] + [_repository(value, "read", args) for value in args.read_repo]
        provider = args.provider or ("openai")
        config = FactoryConfig(
            name=args.name,
            repositories=repositories,
            harness=args.harness,
            coordinator=args.coordinator,
            coordinator_model=args.coordinator_model
            or (f"{provider}:gpt-6.1-terra"),
            auth_mode=args.auth,
            api_key_env=args.api_key_env,
            isolation=args.isolation,
            trust_registered_repositories=args.trust_repositories,
            herdr_space=args.herdr_space,
            slack_webhook_env=args.slack_webhook_env,
            max_recoveries=args.max_recoveries,
            max_revisions=args.max_revisions,
            phase_timeout_seconds=args.phase_timeout,
            command_timeout_seconds=args.command_timeout,
            retry_base_seconds=args.retry_base,
            integration=args.integration,
            github_issue_label=args.issue_label,
            github_poll_seconds=args.poll_seconds,
            github_auto_merge=args.auto_merge,
            close_source_issues=args.close_source_issues,
            worker=ModelProfile(
                provider=provider,
                model=args.model,
                reasoning=args.reasoning,
            ),
            reviewer=ModelProfile(
                provider=(
                    provider
                    if (
                        interactive
                        or "--provider" in args._explicit
                        or (
                            args.auth == "api_key"
                            and master["factory"]["reviewer"]["provider"]
                            == "openai-codex"
                        )
                    )
                    else master["factory"]["reviewer"]["provider"]
                ),
                model=args.review_model,
                reasoning=(
                    args.reasoning
                    if interactive or "--reasoning" in args._explicit
                    else master["factory"]["reviewer"]["reasoning"]
                ),
            ),
            qa_model=ModelProfile(
                provider=(
                    provider
                    if (
                        interactive
                        or "--provider" in args._explicit
                        or (
                            args.auth == "api_key"
                            and master["factory"]["qa_model"]["provider"]
                            == "openai-codex"
                        )
                    )
                    else master["factory"]["qa_model"]["provider"]
                ),
                model=args.qa_model,
                reasoning=master["factory"]["qa_model"]["reasoning"],
            ),
            self_improvement=ImprovementPolicy.model_validate(
                merge(
                    master["factory"]["self_improvement"],
                    {
                        "mode": args.self_improvement,
                        "allowed_areas": args.improvement_area,
                    },
                )
            ),
        )
    config = resolve_repositories(config, args.github_repo)
    home = _home(args, config.name)
    try:
        SecretDefaults.model_validate(
            {
                "provider": args.secrets_provider,
                "age_recipient": args.age_recipient,
                "age_key_file": str(args.age_key_file)
                if args.age_key_file
                else None,
                "generate_age_key": args.generate_age_key,
                "op_vault": args.op_vault,
                "op_account": args.op_account,
            }
        )
    except ValueError as error:
        raise ValueError(
            "secret backend options conflict or are invalid"
        ) from error
    if args.api_key_stdin and args.secrets_provider != "age":
        raise ValueError("API key stdin requires age; 1Password uses --op-item")
    if interactive:
        print(
            json.dumps(
                {
                    "home": str(home),
                    "configuration": config.model_dump(),
                    "secrets_provider": args.secrets_provider,
                    "master_defaults": master["path"],
                },
                indent=2,
            )
        )
        if input("Create this factory? [y/N] ").strip().lower() != "y":
            return {"created": False, "reason": "cancelled"}
    write_config(home, config)
    scaffold_secret_config(
        home,
        args.secrets_provider,
        age_recipient=args.age_recipient,
        age_key_file=args.age_key_file,
        generate_age_key=args.generate_age_key,
        op_vault=args.op_vault,
        op_account=args.op_account,
    )
    from .state import State

    state = State(home)
    result = {
        "created": True,
        "home": str(home),
        "name": config.name,
        "isolation": config.isolation,
        "master_defaults": master["path"],
        "secrets_provider": args.secrets_provider,
        "notice": (
            "Cooperative execution does not enforce an OS sandbox."
            if config.isolation == "cooperative"
            else "Required isolation must pass doctor before worker dispatch."
        ),
    }
    close = getattr(state, "close", None)
    if close:
        close()
    if interactive and config.auth_mode == "codex":
        login = subprocess.run(["codex", "login"], check=False)
        result["login_succeeded"] = login.returncode == 0
    if config.auth_mode == "api_key":
        if args.op_item:
            set_reference(home, config.api_key_env, args.op_item)
        elif args.api_key_stdin:
            set_secret(home, config.api_key_env, sys.stdin.readline().strip())
        elif (
            interactive
            and args.secrets_provider == "age"
            and not os.environ.get(config.api_key_env)
        ):
            set_secret(home, config.api_key_env, _read_secret())
    result["readiness"] = doctor(home)
    return result


def _read_secret() -> str:
    if not sys.stdin.isatty() or not sys.stderr.isatty():
        raise ValueError("use --api-key-stdin or an existing environment value")
    try:
        import termios
    except ImportError as error:
        raise ValueError("use --api-key-stdin on this platform") from error
    descriptor = sys.stdin.fileno()
    original = termios.tcgetattr(descriptor)
    masked = list(original)
    masked[3] &= ~termios.ECHO
    sys.stderr.write("API key (stored privately; input hidden): ")
    sys.stderr.flush()
    try:
        termios.tcsetattr(descriptor, termios.TCSADRAIN, masked)
        return sys.stdin.readline().strip()
    finally:
        termios.tcsetattr(descriptor, termios.TCSADRAIN, original)
        sys.stderr.write("\n")


def doctor(home: Path, verify_auth: bool = False) -> dict:
    config = load_config(home)
    checks = []
    try:
        secrets_status = secret_readiness(home, config.api_key_env)
    except CredentialError:
        secrets_status = {
            "provider": None,
            "configured": False,
            "credential_available": False,
            "verified": False,
        }
    for executable in ("git",) + (
        ("hc", "pi") if config.harness == "pi" else ()
    ):
        checks.append(
            {
                "name": f"executable:{executable}",
                "passed": shutil.which(executable) is not None,
            }
        )
    checks.append(
        {
            "name": "execution-isolation",
            "passed": config.isolation == "cooperative",
            "detail": (
                "Explicit cooperative mode; OS isolation is not implemented."
                if config.isolation == "cooperative"
                else "Required OS isolation is not implemented; dispatch is refused."
            ),
        }
    )
    for repo in config.repositories:
        path = Path(repo.path)
        probe = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            check=False,
        )
        checks.append(
            {
                "name": f"repository:{repo.name}",
                "passed": probe.returncode == 0
                and (Path(probe.stdout.strip()).resolve() == path.resolve()),
            }
        )
        if repo.access == "write":
            for kind in ("checks", "qa"):
                checks.append(
                    {
                        "name": f"{repo.name}:{kind}",
                        "passed": bool(getattr(repo, kind)),
                    }
                )
    if config.coordinator == "model":
        checks.append(
            {
                "name": "model-coordinator-library",
                "passed": importlib.util.find_spec("pydantic_ai") is not None,
                "detail": "Does not perform an inference or verify account access.",
            }
        )
    if config.auth_mode == "api_key":
        checks.append(
            {
                "name": "api-key-reference",
                "passed": secrets_status["credential_available"],
                "detail": f"Credential reference: {config.api_key_env}",
            }
        )
    elif config.auth_mode == "codex":
        codex_home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
        checks.append(
            {
                "name": "codex-credential-file",
                "passed": (codex_home / "auth.json").is_file(),
                "detail": "Presence check only; native keychain storage may differ.",
            }
        )
    native_readiness = {}
    if config.harness == "pi" and shutil.which("pi"):
        from .harness import PiHarness

        adapter = PiHarness(config)
        for role in ("implementer", "reviewer", "qa"):
            status = adapter.readiness(home, role, refresh=False)
            native_readiness[role] = status
            checks.append(
                {
                    "name": f"pi-authentication:{role}",
                    "passed": status["status"] == "ready",
                }
            )
    github_checks, github_status = readiness(config, verify_auth)
    checks.extend(github_checks)
    return {
        "ready": all(check["passed"] for check in checks),
        "checks": checks,
        "factory": config.name,
        "credentials_verified": False,
        "secrets": secrets_status,
        "native_workers": native_readiness,
        "github": github_status,
    }


def _submission(args: argparse.Namespace) -> Submission:
    if args.file:
        if str(args.file) == "-":
            return Submission.model_validate(json.load(sys.stdin))
        return Submission.model_validate_json(args.file.read_text())
    return Submission(
        title=args.title or "",
        instruction=args.instruction or "",
        acceptance=args.acceptance,
        repositories=args.repository,
        source=args.source,
        delivery_key=args.delivery_key,
        depends_on=args.depends_on,
    )


def execute(args: argparse.Namespace) -> dict:
    if args.command == "list":
        from .inventory import list_factories

        selected = args.home or os.environ.get("PLUTO_FACTORY_HOME")
        return list_factories(
            args.root or factory_root(), Path(selected) if selected else None
        )
    if args.command == "defaults":
        return read_defaults(args.defaults_file)
    if args.command in {"setup", "init"}:
        return _setup(args)
    if args.command == "demo":
        from .demo import run_demo

        return run_demo(args.directory, args.scenario, args.max_seconds)
    if args.command == "auth":
        if args.provider == "api_key":
            if args.no_input and not (args.api_key_stdin or args.op_item):
                raise ValueError("use --api-key-stdin with --no-input")
            home = _home(args)
            load_config(home)
            if args.op_item:
                set_reference(home, args.api_key_env, args.op_item)
            else:
                value = (
                    sys.stdin.readline().strip()
                    if args.api_key_stdin
                    else _read_secret()
                )
                set_secret(home, args.api_key_env, value)
            return {"provider": "api_key", "authenticated": True}
        if args.no_input:
            raise ValueError("native login requires an interactive invocation")
        result = subprocess.run(["codex", "login"], check=False)
        return {"provider": "codex", "authenticated": result.returncode == 0}
    home = _home(args)
    if args.command == "doctor":
        return doctor(home, verify_auth=args.verify_auth)
    config = load_config(home)
    from .state import State

    if args.command == "improvements":
        from .improvements import activate, rollback

        state = State(home)
        try:
            if args.operation == "list":
                return {"improvements": state.inspect()["improvements"]}
            if not args.id:
                raise ValueError("this improvement operation requires an ID")
            if args.operation == "activate":
                return activate(state, config, args.id)
            return rollback(state, args.id)
        finally:
            state.close()
    if args.command in {"submit", "inspect"} or (
        args.command == "notifications" and not args.deliver
    ):
        state = State(home)
        try:
            if args.command == "submit":
                submission = _submission(args)
                registry = {repo.name: repo for repo in config.repositories}
                if not set(submission.repositories).issubset(registry):
                    raise ValueError(
                        "submission references an unregistered repository"
                    )
                return state.submit(submission)
            snapshot = state.inspect()
            return (
                {"notifications": snapshot.get("notifications", [])}
                if args.command == "notifications"
                else snapshot
            )
        finally:
            state.close()
    from .engine import Engine

    if args.command in {"pause", "resume"}:
        return getattr(Engine(home), args.command)(args.work)
    if args.command == "notifications":
        return Engine(home).flush_notifications()
    if args.max_seconds is not None and args.max_seconds <= 0:
        raise ValueError("--max-seconds must be positive")
    return Engine(home).run(
        once=args.once, max_seconds=args.max_seconds, watch=args.watch
    )


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    args = parser().parse_args(arguments)
    args._explicit = {
        argument.partition("=")[0]
        for argument in arguments
        if argument.startswith("--")
    }
    try:
        result = execute(args)
        if args.command == "list" and not args.json:
            from .inventory import render_inventory

            print(render_inventory(result))
        else:
            print(json.dumps(result, indent=2, default=str))
        if args.command == "doctor" and not result["ready"]:
            return 1
        if args.command == "auth" and not result["authenticated"]:
            return 1
        return 0
    except KeyboardInterrupt:
        print(
            "pluto-factory: interrupted; durable state is retained",
            file=sys.stderr,
        )
        return 130
    except (ValueError, TypeError, OSError, RuntimeError) as error:
        print(f"pluto-factory: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
