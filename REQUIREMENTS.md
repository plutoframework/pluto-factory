# Prototype requirements and validation notes

The canonical source is the `plutoframework/pluto-factory` repository. The
prototype is local Python code, installed with `uv tool install --editable .`.
The sibling Pluto CLI supplies the `pluto factory` command bridge.

## Implemented boundaries

| Requirement | Implementation and evidence |
| --- | --- |
| Independent factories | Separate TOML bundle, SQLite ledger, locks and owned directories |
| Interactive and headless setup | Shared validation/scaffolding; terminal-only wizard; `--no-input` |
| Master defaults | Optional `~/.config/pluto/pluto.toml`; explicit choices win; resolved settings stay per factory |
| Secrets | Factory-specific fnox configuration; age by default or 1Password references; no plaintext fallback |
| Repository scope | Named registry, managed clones, path checks and declared read/write access |
| Intelligent core | Pydantic AI decisions constrained to currently legal actions |
| Interactive Pi | Core terminal launch, typed reporting extension, lifecycle observation and session rebinding |
| Durable recovery | Dispatch intents, report deduplication, generation fences, bounded retries and preserved worktrees |
| Autonomous default | Recover within authority; notifications are separate from deferral; unrelated work continues |
| Validation | Frozen candidate checkouts, explicit command results, flag matrices and configuration digests |
| Review and QA | Separate worker roles plus independently executed validation commands |
| Multiple repositories | Candidate sets and checks of intermediate combinations before sequential integration |
| Completion | Every acceptance criterion needs current evidence; incomplete increments remain open |
| Human control | Pause/dispatch lock, interruption observations, explicit resume and inspect |
| Notifications | Independent durable Slack outbox; failures remain queued without stopping work |
| Prompt customization | Role Markdown and QA guidance outside code; resolved bundles pinned to assignments |
| Self-improvement | Scoped proposals, optional configured evaluation, activation for later assignments, rollback |
| Intake | Direct CLI submissions or paginated ready-labeled GitHub issues |
| Persistent watcher | GitHub mode waits for more work when its queue is empty |
| Issue prerequisites | Durable source references, out-of-order intake, cycle rejection and merged-completion dispatch gates |
| Factory inventory | Read-only machine registry, lock-based running state, readable default and explicit JSON |
| PR lifecycle | Owned branch publication, PR reconciliation, remote checks, opt-in ordered auto-merge and guarded issue closure |

## Verified examples

- Deterministic fixtures exercise the actual SQLite, Git, checks, review/QA
  handoffs, integration and recovery paths. They do not measure model quality.
- A live Pi worker used the existing subscription sign-in to fix a disposable
  addition bug and add regression tests. Independent checks passed with the
  sample feature flag disabled and enabled.
- The same live Pi terminal accepted a newer assignment generation and emitted
  a second valid report for a verification-only turn. The original source
  repository remained unchanged; the owned test terminal was cleaned up.
- A live Pydantic coordinator call using Terra returned a schema-valid action
  from the supplied legal set, using existing subscription authentication.
- A disposable age/fnox round trip stored ciphertext and retrieved the fake
  test value without passing it in command arguments.
- Native Pluto CLI forwarding tests cover arguments, streams, help, failure,
  and interactive/headless mode propagation. A real setup invocation reached
  its first wizard prompt and was cancelled without creating configuration.

Pi's installed subscription provider is named `openai`. The Pydantic AI Codex
provider has its own `openai-codex` namespace. They must not be inferred from
one another. The user's existing Pi login worked; the initial live test had
selected the wrong provider name.

## Explicit prototype limits

- Host execution is cooperative, not an OS sandbox. Required isolation is
  rejected rather than silently downgraded. Container packaging and migration
  remain follow-up work.
- Pi is the implemented coding harness. Other harnesses can implement the
  contract, but Claude, Codex CLI and Pydantic Coder adapters are not supplied.
- Configured QA commands can run app/browser tests. The QA model role does
  not yet include a general browser/computer-use tool adapter or feature-map
  discovery.
- Herdr visibility uses a configured existing space ID. Automatic space
  creation and a dedicated app takeover control remain follow-ups.
- Headless execution and desktop presentation are separate concerns. A future
  desktop attachment command should connect to a server's persistent Core over
  SSH (including a Tailscale-reachable host), discover factory-labeled sessions,
  and create client-side space mappings. A blank server-side space ID must not
  imply that those sessions are inaccessible. The factory ledger and secrets
  stay on the execution host; display IDs are scoped to the client/Core mapping.
- GitHub operations have offline adapter, dependency, CI-gate and watcher tests.
  The sequential Snake issue-to-PR-to-merge exercise is a separate live proof;
  an ingested issue or dispatched worker is not a completed integration.
- Slack delivery has outbox tests. No real channel has been messaged during
  prototype validation. Incoming Slack decisions and socket intake are not
  implemented.
- One worker assignment runs at a time within each prototype factory.
  Multiple factory processes can run independently.
- Setup records prerequisites honestly. Metadata readiness is not proof that
  every configured model, command, or remote permission will work.

## Snake exercise

The user owns the new Snake repository and its initial push. Decompose the
example into scoped increments before dispatch; do not substitute a whole-game
issue for the multi-issue workflow. The package's
[Snake exercise](src/pluto_factory/examples/snake/README.md) describes the stages.

1. Install the local engine and rebuilt Pluto CLI.
2. Run `pluto factory setup`, select the local Snake repository and GitHub
   integration, and configure actual check/QA commands.
3. Use `doctor --verify-auth` to check the selected prerequisites.
4. Decompose the desired game into scoped GitHub issues with explicit
   acceptance checkboxes, then label selected issues `factory:ready`.
5. Start the watcher and inspect implementation, checks, review, QA and PRs.
6. Verify candidate freshness, remote CI, merge ordering, issue completion and
   restart recovery before expanding autonomy.

Auto-merge is an explicit owner choice and is off by default. Existing
repository review/branch policies still apply.
