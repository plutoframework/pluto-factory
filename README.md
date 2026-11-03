# Pluto factory prototype

The [Pluto factory](https://github.com/plutoframework/pluto-factory)
package runs a durable local work loop with isolated managed Git clones,
candidate checks, separate review and QA roles, typed worker reports, bounded
recovery, and a notification outbox. Pi is the first interactive worker adapter.
The coordinator can use Pydantic AI or deterministic rules for fixture tests.
GitHub integration can ingest ready-labeled issues and publish validated PRs;
managed integration keeps example work entirely local.

The prototype integrates into the **locally rebuilt Pluto CLI** as
`pluto factory`. The Python engine remains a separate package; the Go command
passes argument arrays and streams directly to it. It does not install packages
or run a shell behind the user's back.

## Install and inspect

From this directory:

```sh
uv tool install --editable .
pluto-factory --help
```

After applying the local CLI integration and rebuilding the sibling CLI:

```sh
go -C ../pluto-cli build -o /tmp/pluto-factory-cli ./cmd/pluto
/tmp/pluto-factory-cli factory --help
```

The integration patch is retained at
[`integrations/pluto-cli.patch`](integrations/pluto-cli.patch). It adds no Go
dependencies. `PLUTO_FACTORY_ENGINE` names one executable, not a shell command.

To install the rebuilt Go command as well:

```sh
GOBIN="$HOME/.local/bin" go -C ../pluto-cli install ./cmd/pluto
```

Ensure the installed Python tool and Go binary directories are on `PATH`.
The examples below use `pluto`; substitute `/tmp/pluto-factory-cli` when using
the development build instead of an installed binary.

For development, `uv sync` creates the local test environment. An explicit
`PLUTO_FACTORY_ENGINE` executable overrides the normal `PATH` lookup when needed.

## Run disposable example work

The demo creates two local Git repositories and runs work through the real
coordinator, SQLite state, worktrees, validation, and local integration path.
Its **fixture worker is deterministic and performs no model inference**.
Fixture review and QA reports exercise the handoff contracts; they do not prove
AI review quality.

```sh
pluto-factory demo --directory /tmp/pluto-factory-demo
pluto-factory demo --directory /tmp/pluto-factory-recovery \
  --scenario crash_once
```

Other scenarios are `provider_once`, `malformed_once`, and
`idle_no_report_once`. Each directory must be empty. The demo preserves the
source repositories and integrates changes only into factory-managed clones.

The initial example has passing existing flag tests and an untested addition
bug. Its work item fixes the bug in both repositories and adds the missing
regression test. Checks and QA run with the example flag disabled and enabled.

Inspect or continue an existing example:

```sh
pluto-factory inspect --home /tmp/pluto-factory-demo/factory
pluto-factory run --home /tmp/pluto-factory-demo/factory --max-seconds 30
```

## Master defaults and per-factory configuration

There are three distinct files:

| File | Scope |
| --- | --- |
| `$XDG_CONFIG_HOME/pluto/pluto.toml` | Master Pluto defaults |
| A repository's `pluto.toml` | That application's manifest |
| `<factory-home>/factory.toml` | One factory's resolved configuration |

When `XDG_CONFIG_HOME` is unset, master configuration is
`~/.config/pluto/pluto.toml`. Setup reads it; it never discovers a project
manifest as master configuration. A master file is optional.

Example secret-free master defaults:

```toml
[factory]
coordinator = "model"
coordinator_model = "openai-codex:gpt-6.1-terra"
auth_mode = "existing"
isolation = "required"
trust_registered_repositories = false
max_recoveries = 3
integration = "managed"
github_issue_label = "factory:ready"
github_poll_seconds = 30
github_auto_merge = false
github_required_checks = []
close_source_issues = true

[factory.worker]
provider = "openai"
model = "gpt-6.1-sol"
reasoning = "high"

[factory.reviewer]
model = "gpt-6.1-sol"
reasoning = "high"

[factory.qa_model]
model = "gpt-6.1-terra"
reasoning = "medium"

[factory.self_improvement]
mode = "propose"
allowed_areas = ["implementer_prompt", "qa_guidance"]

[factory.secrets]
provider = "age"
generate_age_key = false
# To use an existing private identity:
# age_key_file = "/private/path/factory-age.txt"
# age_recipient = "age1..."
```

`[factory]` accepts factory configuration defaults, except names and repository
registrations. `[factory.secrets]` selects the per-factory fnox backend. The
default backend is **age**; `1password` is also supported. Master defaults may
contain references and public recipients, never credential values.

Precedence is explicit setup flags, then fields in `setup --config`, then master
defaults, then built-ins. Setup freezes the resulting factory configuration and
fnox provider configuration. Later master edits do not alter existing factories.
Relative master `age_key_file` paths resolve against the master file's directory.

```sh
pluto-factory defaults show
pluto-factory defaults show --defaults-file /path/to/master-pluto.toml
```

## Guided setup or headless setup

```sh
pluto factory setup
# Alias:
pluto factory init
```

The wizard gathers repositories, checks, QA, models, auth choices, secret
backend, Herdr mapping, notifications, and improvement permissions. It shows a
plan and requests confirmation before creating factory files. Piped input and
CI never silently open a wizard.

At the first repository prompt, blank selects the current directory. A path
alone gets a logical name from its directory; `NAME=PATH` names it explicitly.
Blank at the additional-repository prompt finishes the list. Human factory
names such as `Snake Game` become stable names such as `snake-game`.

Use `pluto --interactive factory setup` or `pluto factory setup --interactive`
to enter the guided flow with prefilled arguments. `--interactive` and
`--no-input` cannot be combined.

Headless setup uses the same validators and scaffolding:

```sh
pluto factory setup --no-input \
  --name application \
  --repo app=/absolute/path/to/application \
  --check '["python3","-m","unittest","-v"]' \
  --qa '["python3","-m","unittest","-v"]' \
  --harness pi --coordinator model \
  --isolation cooperative \
  --trust-repositories \
  --secrets-provider age --generate-age-key
```

Use repeated `--repo NAME=PATH` for multiple writable repositories and
`--read-repo NAME=PATH` for reference repositories. The simple flags assign the
same check and QA commands to all repositories; edit each repository's entry in
`factory.toml` for different commands and flag matrices, or supply a complete
factory config with `--config`.

`--trust-repositories` preauthorizes Pi to load guidance from the registered
repositories for each run. Without it, Pi can ask for project trust when a new
managed worktree starts. This does not provide OS isolation.

Owner-permitted prompt changes can be inspected and activated explicitly:

```sh
pluto factory improvements list --factory application
pluto factory improvements activate IMPROVEMENT_ID --factory application
pluto factory improvements rollback IMPROVEMENT_ID --factory application
```

See [requirements and validation notes](REQUIREMENTS.md) for the coverage audit,
live proofs, and remaining prototype boundaries.

Checks and QA are executable **argument arrays**, not shell strings. A missing
required validation command is reported as not ready. Setup does not fabricate
a successful check.

Setup creates editable role prompts, QA guidance and rules, the database, and
factory-owned resource directories. Re-running setup never overwrites authored
configuration or prompts. Its JSON response includes readiness; a created
configuration is not a claim that account access or inference succeeded.

### Execution isolation

This prototype supports explicit **cooperative host execution**. That is not an
OS sandbox. The repository registry scopes factory Git operations and task
assignments, but it cannot constrain an unrestricted shell process.

The default `isolation = "required"` refuses dispatch until a real isolation
adapter exists. Selecting `--isolation cooperative` is an explicit choice for a
trusted local prototype. Container packaging and enforced process isolation are
not implemented yet.

### Authentication and fnox

The coordinator and a native harness can have distinct authentication stores.
For subscription access, use the existing coordinator and native Pi logins.
Setup can run `codex login` when that method is explicitly chosen; it does not
claim the same login automatically configures Pi. The installed Pi catalog uses
`provider = "openai"`, including its native OAuth authentication. Pydantic AI's
coordinator uses its own `openai-codex:` subscription provider namespace. These
are separate adapter settings, not interchangeable names.

For API keys, the factory uses **fnox**, not a plaintext credentials file:

```sh
pluto factory setup --no-input \
  --name api-example --repo app=/absolute/path/to/application \
  --check '["python3","-m","unittest"]' \
  --qa '["python3","-m","unittest"]' \
  --auth api_key --api-key-env OPENAI_API_KEY \
  --secrets-provider age --generate-age-key \
  --isolation cooperative
```

`--api-key-stdin` reads a key from stdin during setup. Alternatively, connect it
later with `pluto factory auth api_key --home <home>` or supply the explicitly
named environment value. Interactive key entry masks input on the existing
terminal; it does not open a hidden terminal. Values are passed to `fnox set`
through stdin with an explicit provider, never as command arguments.

The factory stores ciphertext or external references in `fnox.toml`. A private,
empty `fnox-global` directory prevents merging machine-global fnox secrets.
Inherited fnox configuration/profile overrides are excluded. Only the API key
reference selected by the factory is resolved for coordinator/worker injection.
Values do not enter task packets, snapshots, reports, or routine diagnostics.

With age, `--age-key-file` selects a private existing identity and
`--age-recipient` supplies its public recipient. `--generate-age-key` creates a
dedicated identity with owner-only permissions. Keep identity backups separate
from exported factory state.

For 1Password, use `--secrets-provider 1password --op-vault <vault>` and
`--op-item op://vault/item/field`. This records a reference to an existing item;
the prototype does not create vault items or treat a raw key as a reference.
The `op` CLI and its authentication must be available.

`doctor` inspects configuration and native worker readiness without printing
credentials or requesting inference. Its Pi checks use native no-refresh
metadata. Resolving an API credential for those checks can consult the selected
fnox backend. A real provider-backed work item is the separate proof of model
access. GitHub authentication is checked only with `doctor --verify-auth`.

## Submit, supervise, and intervene

List the factories registered on this machine:

```sh
pluto factory list
pluto factory list --json
pluto factory list --root /alternate/factories
```

The inventory reports each factory's name and location, registered repositories,
role models, coordinator status, current work phase, and last recorded activity.
The default view is a compact table with wrapped details. Use `--json` for the
full machine-readable inventory; both `pluto factory list --json` and
`pluto factory --json list` are supported. Narrow terminals use a stacked view.
Running status comes from the coordinator lock rather than a saved PID. Paused
or deferred work remains visible. Invalid configurations and unavailable
databases are reported per entry instead of hiding other factories.

Listing does not initialize a missing database or modify factory state. It scans
the standard `~/.config/pluto/factories` registry (respecting `XDG_CONFIG_HOME`).
Use `--root` for another registry; `--home` or `PLUTO_FACTORY_HOME` additionally
includes a custom factory outside that registry. Arbitrary filesystem locations
are not searched.

```sh
pluto factory submit --home <home> \
  --title "Repair pagination" \
  --instruction "Fix the pagination regression in the assigned repository" \
  --acceptance "The reproduced regression has a passing test" \
  --repository app --source cli --delivery-key pagination-report-1

pluto factory start --home <home>
pluto factory inspect --home <home>
pluto factory pause --home <home>
pluto factory resume --home <home>
```

`submit --file request.json` and `submit --file -` accept the same typed
submission from agents, bug collectors, or a voice-driven caller. Delivery keys
deduplicate repeated submissions. Dependencies are explicit work-item references.

The prototype runs one active item per factory. Separate factories own separate
databases and coordinator locks. A worker remains assigned until a report or a
recoverable unresolved state is recorded.

Pi stays interactive in Herdr Core. `--herdr-space <id>` maps sessions into an
existing Herdr app space. Without a space mapping, execution can remain headless.
The prototype does not automatically create app spaces or infer human takeover
merely from someone opening a terminal. Use the explicit pause/resume flow.

Show the currently active worker without copying a terminal ID:

```sh
pluto factory open --factory snake
pluto factory open --factory snake --terminal
pluto factory open --factory snake --json
```

The default command peeks the current worker in an existing Herdr window.
`--terminal` instead attaches through the calling terminal for headless or SSH
use. The lookup follows the current assignment and verifies live Core identity,
ownership labels and generation. Paused, deferred and historical workers are
not selected. During checks, integration or idle phases with no active worker,
the command reports the phase instead of opening an old session.

If the Herdr window is unavailable, the command prints a rerunnable terminal
fallback. It does not create an app window, space or tab. This is a visibility
operation and does not pause coordination or change work state. Default output
omits internal IDs; `--json` returns operational metadata.

The reporting extension emits typed assignment-bound results. Settled status
alone does not accept work. The coordinator checks report shape, runtime state,
candidate identity, check evidence, and required findings before moving on.

## GitHub issue intake and PR workflow

Create and push the Snake repository yourself first. After that, decompose the
game into issues with the assistant and decide which issue is ready for the
factory. The factory receives those scoped issues rather than creating the
repository or implementing the whole game during setup.

For a pushed local checkout:

```sh
pluto factory setup --no-input \
  --name snake --repo game=/absolute/path/to/snake \
  --integration github --github-repo game=OWNER/REPOSITORY \
  --issue-label factory:ready --poll-seconds 30 \
  --check '["npm","test","--","--run"]' \
  --qa '["npm","run","test:e2e"]' \
  --harness pi --coordinator model --isolation cooperative

pluto factory doctor --factory snake --verify-auth
pluto factory start --factory snake --watch
```

Replace the example validation commands with commands that the actual Snake
repository provides. Required commands must exist and run meaningful checks.
You can instead use the interactive setup wizard, which asks about integration
and suggests the GitHub slug from the local `origin` URL.

For an empty browser-game repository, this package includes independent Snake
acceptance scripts under `src/pluto_factory/examples/snake`. They specify a pure
`game.mjs` API and a small browser DOM contract, so the factory can build against
real checks before the application exists. Read the scripts when writing the
issue; they are acceptance criteria, not a generated implementation.

`acceptance.test.mjs` uses Node's built-in test runner. `browser-qa.mjs` starts a
temporary local HTTP server, uses Playwright with installed Google Chrome, and
saves desktop/mobile screenshots. It takes an absolute Playwright module path
and an artifact directory as its two arguments. For example, from a candidate
checkout:

```sh
node --test /absolute/path/to/pluto-factory/src/pluto_factory/examples/snake/acceptance.test.mjs
node /absolute/path/to/pluto-factory/src/pluto_factory/examples/snake/browser-qa.mjs \
  /absolute/path/to/node_modules/playwright/index.mjs \
  /absolute/path/to/factory/artifacts/browser
```

Register these same argument arrays as the repository's check and QA commands.
The browser runner requires Node, Playwright and Google Chrome on the execution
host; it does not install them or fetch dependencies during validation. A
production project should own its appropriate test contracts and dependencies.

The adapter uses the existing `gh` login for GitHub API calls and HTTPS Git
operations. Its command-scoped Git credential helper leaves global Git
configuration unchanged and does not put token values in arguments.

`--github-repo` maps a registered local repository name to `OWNER/REPOSITORY`.
Without an explicit mapping, GitHub mode can infer standard GitHub HTTPS or SSH
origins locally. Setup does not query GitHub. An unknown repository mapping or
an unresolved writable repository is rejected before factory files are created.

Each ready issue should describe scope, expected behavior, and explicit
acceptance criteria. Checkbox items in the issue body become acceptance criteria:

```markdown
## Acceptance
- [ ] State the expected observable behavior.
- [ ] State the check that demonstrates it.
```

Dependent issues can declare prerequisites without knowing internal work IDs:

```markdown
<!-- pluto-factory:{"repositories":["game"],"depends_on":["#2","OWNER/REPO#3"]} -->
```

Local references use the source repository; cross-repository references must
name registered repositories. Missing prerequisites remain waiting. The
dependent issue becomes eligible after all prerequisites are merged and marked
completed, rather than merely reported ready or published as PRs. Cycles and
self-dependencies are rejected. Invalid intake is recorded without starving
healthy work. Direct JSON submissions support `depends_on_sources`; existing
`depends_on` entries continue to refer to internal work IDs.

The [sequential Snake exercise](src/pluto_factory/examples/snake/README.md)
demonstrates small increments with these dependencies and feature flags.

Apply the configured ready label only after it is ready
for autonomous work. For example, after creating the label in the repository:

```sh
gh issue edit ISSUE_NUMBER --repo OWNER/REPOSITORY --add-label factory:ready
```

The watcher brings matching issues into the same durable queue as `submit`.
It remains running when the queue is empty and checks again at the configured
interval. `start` defaults to watching for GitHub factories; `--no-watch` drains
current work and exits. `--once` performs one intake/tick cycle. Managed demos
continue to exit when drained unless watch mode is explicitly requested.

Inspect queue progress, recorded evidence, and resulting PR references with:

```sh
pluto factory inspect --factory snake
```

GitHub mode publishes validated changes to PRs. Automatic merging is disabled by
default. Use `--auto-merge` only when the repository's policy permits it; remote
required checks and review requirements still apply. `--no-auto-merge` overrides
an inherited automatic-merge setting. Source issues close only according to the
configured completion/integration policy, not merely because a worker reports
that it finished coding.

Set `github_required_checks = ["validate"]` in the factory configuration when
automatic merges must wait for a named CI job. Missing or non-successful named
checks withhold merge requests, as do observed pending/failed checks at the
validated head. This also covers the initial empty rollup before CI starts.
The adapter never uses an administrator merge bypass.

## Recovery, notifications, and improvement

Recovery runs autonomously within configured authority and budgets. Unknown
failures enter bounded diagnosis/recovery. Notifications and execution blocking
are separate decisions. Uncertain launches or external outcomes are reconciled
before repetition; failed worktrees and useful artifacts are retained.

Notifications enter a durable outbox. Set `slack_webhook_env` to an explicitly
configured environment reference, then use `notifications --deliver` to deliver
outbound messages. No demo sends to Slack. Slack approvals and inbound commands
are not implemented.

Prompts and QA configuration are editable outside code. Runs pin their resolved
configuration. Improvement suggestions are recorded; activation must follow the
configured area permissions and evaluation path. Changes to permissions,
credentials, evidence, or merge authority are not worker prompt edits.

## Prototype boundaries

Managed integration advances the default branch in a managed clone without
pushing into the source repository. GitHub mode is a separate adapter for issue
intake and PR publication. Live GitHub publication and merge behavior require
their own provider-backed verification; fixture tests do not prove them.
Release/deployment, additional bug-source adapters, Slack replies, and portable
container export/import remain follow-up adapters.

Cross-repository work records per-repository candidates and integration results;
there is no atomic merge across repositories. Every intermediate state needs
compatible behavior. Passing checks and a typed report do not prove absence of
defects, nor do fixture reports establish model judgment quality.

## Verify

```sh
uv run pytest
uv run ruff check src tests
uv run ruff format --check src tests
```

The tests use disposable repositories and isolated configuration homes. Real
age/fnox tests use disposable fake tokens, not account credentials. Live Pi and
model-provider integration must be reported separately from fixture tests.
