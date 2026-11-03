# Sequential Snake Arcade exercise

This example tests repeated issue-to-PR cycles. It is deliberately split into
small increments rather than one whole-game issue. The game implementation
belongs in a separate repository, not in the factory package.

| Increment | Scope | Prerequisite |
| --- | --- | --- |
| Foundation | Shell, flags, module contracts, test/QA commands and CI | None |
| Engine | Deterministic pure state transitions | Foundation |
| Interface | Canvas, keyboard/touch, pause and restart | Engine |
| Persistence | Versioned local scores/settings and failure recovery | Interface |
| Challenges | Seeded boards, obstacles and difficulty | Engine |
| Replay | Validated tick-indexed recordings/playback | Engine |
| Integration | Cross-feature flows and accessibility | Persistence, challenges, replay |
| Acceptance | Full regression, screenshots, docs and finished defaults | Integration |

Execution stays sequential. Prerequisite PRs must merge before dependent work
is eligible. Automatic merging is an explicit per-factory owner choice.

Each increment adds its own tests and extends the existing QA suite. Use
`npm test` and `npm run qa` throughout; future capabilities stay behind flags
until complete. Run validation with `SNAKE_FEATURES=none` and `all`. An initial
shell must remain runnable while later modules are absent.

The included `acceptance.test.mjs` and `browser-qa.mjs` are independent final
game checks. They are unsuitable as mandatory gates for the foundation issue,
which intentionally does not implement gameplay. The final browser check uses
Playwright and installed Google Chrome; its arguments are the Playwright module
path and screenshot directory.

For development QA, a project runner can accept `PLUTO_PLAYWRIGHT_MODULE`,
`PLUTO_BROWSER_CHANNEL` and `PLUTO_QA_ARTIFACT_DIR` overrides. CI should install
the project's locked development dependencies and browser, then run the same
test and QA commands without machine-specific paths. Runtime browser assets
remain dependency-free.
