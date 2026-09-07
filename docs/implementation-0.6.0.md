# Bello 0.6.0 implementation record

Status: local development checkpoint; native-platform and live-account acceptance
remain open. Not a release or a claim of feature parity.
Base: `v0.5.2`, `a6ccb236354dd6a0f6230da75dd8d0b380ba9656`.
Branch: `codex/0.6.0-multi-provider`. No main changes or publication authorized.

## Agreed scope

Replace the Codex app-server executor with Pi for supported subscription/API
providers. Use the official Claude Code execution path for the user's included
Claude subscription, where Pi's direct API authentication is not equivalent.
Keep one Bello product, with Codex and Claude Code front-end plugins. Other
front ends can be added later. This is a full implementation, not a pilot or a
new optional experimental mode alongside the original executor.

Preserve the coder, runtime supervisor, completion review, adversary and revision
coder lifecycles; prompt intent; existing approval and readiness decisions;
private planning; disposable workspaces; final patch handback; recovery; and
subagent concurrency, lineage, profile enforcement and shutdown. Every role and
allowed subagent profile can choose its provider. Never silently change a model,
reasoning effort, billing route or sandbox strength.

Advisor changes are deferred until the executor works. Benchmark results, README
marketing text, unrelated experiments and user files are out of scope.

## Implementation boundaries

The controller retains its existing role/gate logic. A Bello-owned runtime client
normalizes provider events into the existing internal thread/turn/item contract.
Pi's SDK runs in a pinned Node worker, with all ambient extensions, tools, skills,
project instructions and automatic fallback models disabled. Model tools go
through Bello's common tool host and an actual OS sandbox. Authentication stays
in the trusted provider process, outside the model's tool filesystem.

Claude Code uses its official client and the same Bello-managed tools. No copied
subscription tokens, unofficial API billing, native unmanaged subagents or
permission bypasses. Unsupported capabilities must be surfaced, not emulated by
guessing or quietly weakening an existing safety property.

## Work and acceptance checklist

- [x] Resolve and create a separate branch from exact v0.5.2.
- [x] Record the baseline test outcome without paid model calls: 955 passed,
  7 skipped in 29.47 seconds on macOS/Python 3.14.
- [x] Implement and test the pinned Pi SDK bridge, session/event/usage handling.
- [ ] Implement and test isolated command/filesystem operations on each platform.
- [x] Implement the official Claude Code subscription adapter and SDK protocol tests;
  authenticated live verification remains a separate acceptance item below.
- [x] Integrate neutral runtime routing and existing approval/gate handlers.
- [x] Implement managed cross-provider subagents with existing limits and cleanup.
- [x] Add provider-aware configuration, capabilities and precise effort handling.
- [x] Replace the production Codex executor's preflight, installation and recovery
  assumptions. Existing internal DTO/helper names are retained for compatibility.
- [x] Package runtime sources, dependency locks and third-party license notices;
  verify local wheel and source archives. Native Windows wheel execution awaits CI.
- [x] Package Codex and Claude Code delegation integrations; advisor afterward.
- [x] Run the full local regression suite plus protocol, cancellation and recovery
  tests. Native platform enforcement is tracked separately above.
- [ ] Exercise real local/supported-platform smoke tasks and document limitations.
- [x] Review the local diff and record remaining acceptance work; do not push
  or release without further authorization.

## Verification discipline

Check event ordering, stale generations, missing approvals, cancelled requests,
unknown tools, lost connections, partial writes and process-tree cleanup. An
approval is permission for the exact action, not a global disabling of safety.
Recovery must not replay an uncertain command. A structured reviewer response
is accepted only after the existing schema validation. Test failures and
platform limitations remain visible until resolved.

## Current verification notes

- The default controller now routes through `RuntimeClient`, not a Codex
  app-server process. Its internal thread/turn/item names remain compatible
  with the existing controller. All provider tools pass through `ToolHost`.
- Pi 0.85.1 is pinned with its npm lockfile. Its SDK worker has real offline
  bootstrap coverage and structured-schema tests using Bello's actual schemas.
- The optional official Claude Agent SDK is pinned at 0.2.152. Its adapter has
  isolated SDK control/auth/schema tests. No subscription tokens are copied.
- Regression checkpoint: 1176 passed, 8 skipped and one explicitly expected
  macOS cleanup limitation in 46.95 seconds; pinned Pi worker tests: 26 passed.
  Native Windows enforcement still needs its own runner; those assertions are
  not claimed by the cross-platform protocol tests.
- The offline integration uses the actual Pi SDK, a loopback-only controlled
  HTTP provider, real ToolHost operations and native macOS isolation. Two
  simultaneous sessions complete writes, reads, commands and strict reviewer
  responses with exact usage records. It is not a model-quality test.
- A complete offline C+A integration passes through the real BelloController,
  RuntimeClient, Pi SDK and sandbox: implementation, a real passing pytest,
  runtime oversight, independent completion inspection, adversarial testing,
  adversary-report review and final patch handback. Only provider responses are
  scripted on loopback. Existing readiness/review gates are not monkeypatched.
  Both non-reasoning/off and reasoning-required/high variants pass; the latter
  checks every outgoing provider request for the exact high effort.
- Managed command sessions support yielding, polling and stopping with bounded
  model-visible output. Cancellation and turn completion await command cleanup;
  recovery marks unknown process outcomes as lost rather than replaying them.
- Failed or unacknowledged thread/turn creation is fenced and cleaned up.
  Child-cleanup failures cannot skip sibling or parent interruption. Provider
  call IDs can repeat in later turns without colliding with durable dispatch.
  A broken Pi event stream terminates its worker; affected host turns and
  yielded commands are fenced before notifying the controller, and cleanup
  includes children running through the other engine.
  Cleanup includes an idle parent whose child is still active. Interrupt,
  archive and shutdown finish their owned cleanup before propagating caller
  cancellation, including repeated cancellation. A transport error callback
  can stop the runtime without waiting on itself.
- Explicit efforts are sent at thread creation, not only turn start. OpenAI
  aliases such as minimal mapped to low are not offered as exact native
  efforts. Omitted effort uses a reported engine default. Non-reasoning API
  profiles can save and reload off; text-only models cannot invoke view_image.
- Codex and Claude Code manifests share one delegation skill and background
  launcher. Both manifest validators and the skill validator pass. No plugin
  has been installed, published or added to a marketplace by this change.
- Native macOS checks proved contained writes, read-only enforcement, sibling
  and symlink isolation, hidden runtime files and network denial. A reproduced
  private-state escape through renaming its `.codex` parent is blocked, with a
  regression covering ancestor rename and hard-link aliases. Deliberately
  detached `setsid`/double-fork processes are not fully terminated by process
  group cleanup; this is also a baseline Codex/macOS limitation, not a claim of
  universal process-tree termination. Keep the explicit regression visible.
- Native Windows sandbox source review and local checks are complete: rustfmt,
  three host protocol unit tests, host clippy, Windows-target cargo check and
  Windows-target clippy pass. Execution on Windows is still pending. Source
  cross-compilation does not establish runtime
  enforcement. Cleanup, ACL restoration, process termination, path handling and
  large-directory overhead require native verification before claiming parity.
  The exact-read LPAC design also needs WRITE_DAC on its granted runtime roots:
  ordinary-user operation with machine-wide Program Files toolchains must be
  tested, not inferred from an administrator CI session. No elevation or weaker
  filesystem boundary is silently substituted.
- Windows bridge tests exercise the real subprocess/pipe lifecycle with a fake
  helper: framing, bounded output, strict control messages, cancellation,
  timeout, parent EOF and recovery failures. They are not Win32 enforcement
  tests. Source-distribution and wheel tests verify runtime assets and exact
  license notices while excluding dependency caches and local experiment data.
- CI definitions cover native Windows 2022/2025 and a non-administrator account.
  Release packaging is prepared to build a separate Windows wheel containing
  the mandatory native helper; publication waits for both portable and Windows
  artifacts. No workflow has been dispatched and no release has been created.
- Read-only authentication checks found no configured Pi provider and no
  signed-in official Claude Code account. Live model smoke tests have not run.
  They require the user's normal login; mocks/offline tests are not substitutes
  for those live checks.
