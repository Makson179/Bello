# Bello 0.6.0 implementation record

Status: development checkpoint at `a99d556`; system-resolver DNS is an explicitly
accepted Windows 0.6.0 limitation, not a fixed issue or a release blocker. Final
release verification remains separate. Not a release or a claim of feature parity.
Base: `v0.5.2`, `a6ccb236354dd6a0f6230da75dd8d0b380ba9656`.
Branch: `mystery`. Review/CI changes are pushed there, not released to main or PyPI.

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

The advisor is now migrated to the multi-provider catalog and recommends one
concrete setup. Benchmark results, README marketing text, unrelated experiments
and user files are out of scope for this implementation record.

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
- [ ] Complete final native acceptance against the documented platform boundaries,
  including the accepted Windows system-resolver DNS limitation below.
- [x] Implement the official Claude Code subscription adapter and SDK protocol tests;
  authenticated live verification remains a separate acceptance item below.
- [x] Integrate neutral runtime routing and existing approval/gate handlers.
- [x] Implement managed cross-provider subagents with existing limits and cleanup.
- [x] Add provider-aware configuration, capabilities and precise effort handling.
- [x] Replace the production Codex executor's preflight, installation and recovery
  assumptions. Existing internal DTO/helper names are retained for compatibility.
- [x] Package runtime sources, dependency locks and third-party license notices;
  verify wheel/source assets and execute the packaged helper on native Windows.
- [x] Package Codex and Claude Code delegation and provider-aware advisor integrations.
- [x] Run the full local regression suite plus protocol, cancellation and recovery
  tests. Native platform enforcement is tracked separately above.
- [ ] Finish supported-platform release acceptance; actual local/native smoke
  checks have run, and updated DNS characterization still needs its CI result.
- [x] Review the implementation diff and record remaining acceptance work on
  the separate review branch; publication remains a separate step.

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
- Current local checkpoint: 1506 pytest tests and six Pi SDK integration tests
  passed. All seven general CI jobs passed at `a99d556`.
  At `a99d556`, native Windows Server 2022 CI is fully green; Server 2025 fails
  only the previous full-DNS-denial assertion below. This records the actual run,
  not a passing result for the revised, accepted DNS boundary.
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
- Codex and Claude Code manifests share the delegation skill, background
  launcher and migrated advisor. Manifest/skill validation passes. The advisor
  reads Bello's catalog, preserves provider/billing identities and returns one
  setup. Publishing and installing the updated marketplace package are separate
  release steps.
- Native macOS checks proved contained writes, read-only enforcement, sibling
  and symlink isolation, hidden runtime files and network denial. A reproduced
  private-state escape through renaming its `.codex` parent is blocked, with a
  regression covering ancestor rename and hard-link aliases. Deliberately
  detached `setsid`/double-fork processes are not fully terminated by process
  group cleanup; this is also a baseline Codex/macOS limitation, not a claim of
  universal process-tree termination. Keep the explicit regression visible.
- Actual Windows Server 2022/2025 runs have passed filesystem/private-state
  isolation, ACL cleanup, Node commands, an unactivated Python venv, NUL
  read/write and normal-user smoke checks. These are native executions, not
  cross-compilation results. Explicit administrator setup and subsequent
  ordinary-user execution are tested separately; unsupported toolchain
  locations still fail closed rather than silently elevating.
- Direct per-package WFP traffic tests pass for TCP/UDP over IPv4 and IPv6.
  However, an actual Windows Server 2025 test receives a DNS query at the local
  responder through the Windows DNS Client (`Dnscache`) service while the caller
  is configured offline. System-resolver DNS is therefore not fenced: query
  names can disclose domains or encode data the command is allowed to read.
  This confirmed behavior is explicitly accepted as a Windows 0.6.0 limitation,
  not repaired or hidden. Direct-connection blocking is not complete network
  isolation or protection against DNS-based data exfiltration.
- Windows Server 2022 job `103465550916` at `a99d556` passed 97 Rust tests,
  both serial broker lifecycle tests, 77 Python tests (21 platform skips), six
  Pi/C+A integrations, normal-user checks, exact setup restoration and the final
  gate. This includes the broker quiesce/stop fix; it does not certify Server 2025.
- At the same revision, Server 2025 also passed both serial broker lifecycle
  tests, 77 Python tests (21 platform skips), six Pi integrations, normal-user
  service-control denial and all setup restoration checks. Its Rust suite had
  96 passes and one DNS failure; the final gate was red under the previous
  full-DNS-denial assertion. These results remain historical facts; accepting
  the limitation does not change the recorded outcome or claim a fix.
- Windows bridge tests exercise the real subprocess/pipe lifecycle with a fake
  helper: framing, bounded output, strict control messages, cancellation,
  timeout, parent EOF and recovery failures. They are not Win32 enforcement
  tests. Source-distribution and wheel tests verify runtime assets and exact
  license notices while excluding dependency caches and local experiment data.
- CI runs on native Windows 2022/2025 include a non-administrator account.
  Release packaging builds a separate Windows wheel containing the mandatory
  helper; publication still requires accepted portable and Windows artifacts.
- The six Pi integration checks at this checkpoint use scripted local-provider
  responses, including C+A through the real controller, SDK, tools and sandbox.
  No fresh paid model calls were made for this checkpoint. Earlier account-backed
  OpenAI/Claude/OpenRouter smoke runs are separate historical evidence; these
  local checks neither replace them nor establish current live-account,
  billing-route or model-quality acceptance.
