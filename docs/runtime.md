# Multi-provider runtime

This document describes the `0.6.0` release candidate. It is not an
announcement of a published release. Platform and live-account verification is
tracked in [the implementation record](implementation-0.6.0.md).

## One Bello pipeline, three execution paths

Bello still owns the coder, runtime supervision, completion review, adversarial
testing, revision coder, private plans, disposable workspaces and final patch.
An execution engine supplies the model conversation; it does not decide Bello's
approvals or completion gates.

| Model selection | Engine | Authentication |
| --- | --- | --- |
| `openai-codex/<model>` | Native Codex app-server | The user's own Codex ChatGPT login |
| `openai/<model>` | Pi | OpenAI API credentials |
| `anthropic/<model>` | Pi | Anthropic API credentials |
| Other Pi `provider/model` identifiers | Pi | That provider's supported authentication method |
| `claude-code/<model>` | Official Claude Code through its Agent SDK | The user's own Claude subscription login |

Existing unqualified `gpt-*` selections keep the `openai-codex` subscription
route. They do not become API calls. Bello never changes the provider or billing
route to work around a missing login or unavailable model. Provider-specific
limits and billing rules still apply.

Pi 1.0.4 also offers ChatGPT OAuth under its `openai` provider. Bello deliberately
does not use that method for `openai/*`: these selections retain API-key billing.
A stored OpenAI OAuth credential is reported as a route conflict, even if an API
key is also present in the environment. Bello does not renew, overwrite, or fall
back from that credential. The guard applies before session creation and on each
SDK credential read, including requests from an existing session. To use ChatGPT
subscription access, select native `openai-codex/*`; to use Pi OpenAI API access,
explicitly configure that provider with an API key. Other Pi providers retain
their existing authentication methods.

Codex retains its own base instructions, native tools and conversation loop.
Bello forwards native approvals and adapts thread/turn identities for its role
controller. Configured cross-provider subagents use Bello's delegation tools;
native unconstrained subagent spawning is disabled. Claude Code and Pi retain
their existing integrations. A saved Pi subscription conversation cannot be
silently migrated into a native Codex session: start a fresh run after changing
to this version.

Install Codex separately for subscription roles and authenticate with
`bello runtime login openai-codex` (the native `codex login` flow).
`BELLO_CODEX_BINARY` may select an explicit Codex executable. This does not
replace the system CLI. Pi installation is needed only for Pi providers.
For the current scoped Linux backend, select the actual native executable,
not an npm JavaScript launcher. The sandbox grants read access to that exact
executable and its resolved symlink target so Bubblewrap can re-execute it;
it does not open the parent installation or the user's home directory.

Native Codex log distillation requires a compatible patched Codex executable:
the selection hook must replace the native tool result before it enters the
conversation, including Code Mode. Stock Codex is supported with distillation
off. With distillation on, missing native selection support fails before a
model turn; Bello never silently falls back to Pi or pretends to compress.
On macOS Apple Silicon, Windows x86-64, and Linux x86-64 with glibc 2.35+,
Bello automatically downloads and verifies a pinned Codex 0.153.4 helper before
the first distilled native run. An alternate build selected with
`BELLO_CODEX_BINARY` requires an explicitly trusted local
`BELLO_CODEX_SELECTION_MANIFEST`. Bello verifies the hash, selection protocol
and bridge deadline before use. The bridge allows at least 315 seconds around
the selector's 300-second limit. The helper is cached separately; it does not
replace the user's global Codex. See [native selection setup](native-codex-selection.md).
Both arms of a comparison must use the same native binary.
The verified hook covers native command and polling text results; it does not
claim to compress images or every native tool type. Existing native output
budgets run before selection, and native execution metadata is preserved.

Native Codex uses its own sandbox. Bello maps the assigned workspace and readable
task/dependency roots into a native permissions profile, adding the native
`:minimal` system paths, not full-disk read access. Writes remain in the assigned
workspace and a private tool scratch directory, with native protection for
`.git`, `.agents` and `.codex`. Subsequent turns inherit that profile rather than
replacing it with a broader legacy sandbox policy. Exact system/runtime readable
paths are native-platform behavior, not a promise of identical Pi isolation.
On native Windows this uses the elevated, dedicated-user sandbox. Read roots
grant access through Windows ACLs; they do not revoke existing access to files
that ordinary Windows users can already read. The outside-write and private-file
checks therefore do not claim that every public file outside the workspace is
unreadable. Host directories writable by Everyone are also an upstream sandbox
limitation, not something log distillation fixes.

The native Codex home is retained under private run state so persisted rollout
paths remain valid after stopping and restarting Bello. It is outside the tool
read/write roots and is not the system Codex configuration directory.

## Development installation

Use Python 3.11 or newer and Node.js 22.19 or newer. From this checkout:

```sh
python -m pip install -e '.[claude]'
bello runtime install
bello doctor
```

The Claude extra pins the official Agent SDK. It can be omitted when no role
uses `claude-code`. This source tree pairs `claude-agent-sdk==0.2.164` (whose
bundled CLI is 2.1.292) with the official Claude Code **2.1.293** release needed
for Haiku 5.5. `bello runtime install claude-code` (also run by `bello update`
and before a Claude run) downloads that exact build from `downloads.claude.ai`
on supported macOS, Linux (glibc) and Windows platforms, checks the platform's pinned
size and SHA-256, and keeps it in Bello's private runtime cache. It takes
precedence over the SDK's older bundle; both the installed SDK version and its
declared bundled-CLI version must match the explicit pairing. No arbitrary
CLI path or automatic fallback is accepted. See
[Windows](windows.md#claude-code-subscription-on-native-windows). Pi's exact dependency versions
are installed from the bundled npm lockfile; the Node dependency tree is not
copied into the Python wheel. `BELLO_NODE` can select an existing compatible
Node executable. `BELLO_RUNTIME_DIR` can select the private Pi installation
cache. The worker pins Pi SDK 1.0.4 and verifies the installed version before use.
Login, catalog discovery and execution resolve the Pi authentication/config
directory in this order: nonempty `BELLO_PI_AGENT_DIR`, nonempty
`PI_CODING_AGENT_DIR`, then `~/.pi/agent`. Relative overrides resolve from the
launcher's working directory; `~` expands to the user's home directory.

When a Claude Code turn fails, Bello reports the complete error text: the full
exception chain (including every member of an exception group), followed by
everything the Claude Code CLI wrote to stderr during that turn. Nothing is
truncated, filtered or redacted. The text can therefore contain account details or
credentials, so treat logs and run state like credentials. The text appears in the
run's events, the final report and the terminal. Every CLI stderr line is also
appended to `.supervisor/engines/claude/claude-cli-stderr.log` (mode 0600, one
timestamped line per entry with its thread). The failure diagnostic kept in run
state records each exception's type, message, frames and the full traceback, plus
that stderr. A failing `claude auth status --json` reports its exit code, stderr and
stdout.

The same applies when the CLI ends a turn with an error result instead of failing
(for example an API error such as an overloaded or unreachable API): the turn error
names the result's subtype, terminal reason and HTTP status and contains its
complete error text. Other engines (native Codex, Pi) keep their existing bounded,
redacted provider errors.

Before the first query of a turn, a timeout while connecting, initializing or
reading context usage is retried once on the same session after closing the first
connection. No prompt is resent; authentication and other errors are not retried.

Linux restricted execution requires `bwrap` (the `bubblewrap` package) and
working unprivileged user namespaces. Ubuntu 26.04's Rust coreutils (uutils) are
supported: Bello still recognizes `/bin/cat`, `sha256sum` and similar system readers
when they resolve to the administrator-installed multicall binary. macOS uses the
operating system's Seatbelt sandbox. Sandboxed commands run with
`GIT_CONFIG_NOSYSTEM=1` and `GIT_ATTR_NOSYSTEM=1`, so `git` ignores the system-wide
git configuration and attributes (as on Linux, where neither file is mounted into
the sandbox). The global configuration comes from the sandbox's private home.
Native Windows uses Bello's packaged LPAC helper. Windows source builds
require Rust/Cargo; Windows release wheels include the compiled helper. LPAC is
Windows process isolation with restricted filesystem and network permissions.
Native Windows verification is tracked separately; passing non-Windows tests
does not certify the Windows boundary.

**Known Windows 0.6.0 limitation:** offline commands are blocked from direct
TCP/UDP connections over IPv4 and IPv6, but system DNS resolution through the
Windows DNS Client (`Dnscache`) service is not fenced. Actual Server 2025 tests
confirm that it can send a query on an offline process's behalf. Queries can
disclose domain names, and a command can encode data it is allowed to read into
those names to transmit it outside the sandbox. This limitation is accepted for
0.6.0, not fixed; offline mode must not be treated as complete network isolation
or a guarantee against data exfiltration. See the implementation record for
verification status.

Windows tools such as Node and CMD also need metadata access to the system-drive
root and Windows' user-profiles directory (usually `C:\` and `C:\Users`).
Check the one-time preparation without changing permissions:

```powershell
bello runtime windows-sandbox status
```

If preparation is missing, open a terminal with **Run as administrator** and run
`bello runtime windows-sandbox prepare`. It asks for confirmation before adding
the fixed permission. Close that terminal afterwards and run tasks normally,
without administrator rights. Installation and ordinary runs never silently
elevate or perform this setup.

If your project or toolchain is on another local fixed drive, prepare just that
drive's root as well. For example, from the administrator terminal:

```powershell
bello runtime windows-sandbox status --drive D:
bello runtime windows-sandbox prepare --drive D:
```

`--drive D:` selects only `D:\`, not the system directories or other drives.
It accepts a drive letter, not a directory path. Network and substituted drives
are not supported. Use `remove --drive D:` to undo that drive's preparation.

Python test capture and shell redirection also use the fixed `NUL` device: reads
return empty input and writes are discarded. Check it separately:

```powershell
bello runtime windows-sandbox status --null-device
```

If missing, explicitly run `bello runtime windows-sandbox prepare --null-device`
in the administrator terminal. This grants only Bello's named capability
read/write access to `\Device\Null`, not to files or other devices. It does not
change the device's owner or integrity label. Windows resets this permission on
reboot, so check and, if needed, repeat preparation after restarting Windows.
`remove --null-device` removes only this permission. This selector cannot be
combined with `--drive`. No service or scheduled task prepares it automatically.

Directory preparation grants only attributes, extended attributes, permission-descriptor reads
and synchronization on those two directories. It does not grant listing,
file-content reads, writes, or inherited access to descendants. The permission
persists until explicitly removed with `bello runtime windows-sandbox remove`
from an administrator terminal; stop runs before removal. Existing unrelated
permissions are preserved. The named capability is not an authentication check:
another process can request the same capability. Its access remains limited to
this fixed metadata permission, rather than applying to all AppContainers.

Preparation does not grant access to unselected drives or protected machine-wide
toolchains. Exposed toolchain roots must still allow the invoking account to
manage their exact permissions; use host-controlled per-user installations.
Other required parent directories receive temporary metadata-only permissions
for the individual command, when Windows permits the invoking account to modify
their permissions. These do not allow listing or access to sibling files and
are removed during cleanup.
Unsupported locations are rejected rather than triggering automatic elevation.

## Sign in and choose models

```sh
bello runtime login openai-codex
bello runtime login claude-code
bello runtime models
bello config
```

These login commands open each engine's normal sign-in flow. Bello does not
extract a token from another installed coding agent. Pi API providers can be
configured with their supported Pi login method or provider credentials; inspect
the configured catalog with `bello runtime models --engine pi`.

Pi discovery reads local metadata without refreshing OAuth credentials,
executing credential commands, writing model caches or making provider
requests. To reread local auth, model configuration and cached catalog metadata
explicitly, use `bello runtime models --engine pi --refresh`. Refresh is rejected
while the same worker is starting or running a session/turn. Output includes
`catalogFreshness.pi` with a load timestamp, SDK version, `explicit-local`
refresh policy and `remoteFreshness: unknown`. This timestamp describes a local
read, not a successful login, quota check or remote provider refresh. Model
execution and explicit login retain their normal authentication behavior.
Refresh publishes a validated replacement atomically. A failed refresh keeps
the previous catalog visible and blocks new execution until refresh succeeds.
Session creation checks that model configuration still matches the catalog;
changed endpoints or capabilities require refresh, and an existing session
whose model metadata changed requires a fresh session.

New configurations default the four primary roles to GPT-6 Astra at `xhigh`
through the existing Codex subscription route. Saved selections, including
GPT-5.6 variants, remain unchanged; omitted model fields in older saved files
retain the historical GPT-5.6 Sol default. Children and command triage retain
their existing defaults. A newer model is selectable only through its actual
discovered provider identity and capabilities.

As checked October 6, 2026, [OpenAI announces GPT-5.5 retirement from Codex with
ChatGPT sign-in on October 14, 2026](https://developers.openai.com/codex/models#gpt-55-retirement).
Bello warns on that subscription selection without replacing it. The announced
retirement does not apply to the OpenAI API model.

The Claude subscription route rejects environment variables that redirect it to
an API key, alternative endpoint or cloud provider. It does not silently clear
those variables or fall back to pay-per-token API usage. Use an explicitly
selected Pi API provider when API billing is intended.

The editor shows models and reasoning efforts reported by the authenticated
engines. Preflight validates every active role and allowed child profile before
starting model work. Unsupported efforts or Fast mode are errors, not silent
substitutions. In particular, `ultra` is not treated as a synonym for `max`.

`bello config` runs the same offline checks as that preflight step (model
availability, advertised effort and the Fast service tier for exactly the active
profiles) and marks the affected row. STATUS says "Ready / Offline checks
passed" only when they pass; login, sandbox and structured-output checks still
run when a task starts. Engines that could not list models are shown with a
short, sanitized reason and the next command. Claude Code models are shown by
exact identity (`Sonnet 5.5 - claude-code/claude-sonnet-5-5`); a floating alias
such as `claude-code/sonnet` also shows what the installed CLI resolves it to now,
and a run reports that resolution. Saved ids missing from the current catalog,
such as an older `...[1m]` Opus id, are kept and flagged, never replaced; their
absence from the catalog is not treated as evidence of a smaller context window.

Changing a role's model keeps a supported effort. Otherwise Bello uses the
nearest supported level at or below the previous one (the lowest level if none
is lower), never the most expensive by default, and shows the change. A model
whose catalog advertises no effort (for example Claude Haiku through Claude
Code) uses the effort value `default`, which sends no effort parameter; an
unknown catalog is not treated that way.

`speed=fast` (`--fast`) is Bello Fast: it requests the OpenAI/Codex `priority`
service tier for coder, revision-coder, runtime and completion-review turns and
the subagents they start. It is not Smart Execution (`async_tools`) and not
Claude Code's differently named fast mode. Every active profile must offer the
tier; Claude Code subscription models do not, so the editor flags that
combination and preflight rejects it.

For supported OpenAI Codex models, the pinned Pi integration has an explicit
`ultra` provider-payload mapping. The provider receives literal `ultra`, even
though Pi's internal thinking-level list stops at `max`. Capability metadata
reports this mapping. No such mapping is assumed for arbitrary API providers.
The catalog distinguishes a selected effort from the provider's actual control.
For adapters that use token budgets or other provider-specific controls, it
reports that distinction instead of claiming a literal native effort value.
An omitted effort uses the engine's reported default; an explicit effort is
validated before creating the role's session.

## Independent run controls

Runtime supervision, completion review, adversarial testing and the local log
distiller have independent switches in `bello config`. Runtime defaults to on;
completion review, adversary and the distiller default to off. Turning completion
review off preserves the adversary setting. A revision coder can handle feedback
when either completion review or adversary is enabled.

Outside Goal, without an explicit readiness marker, automatic final review waits
for **40 minutes of continuous coder inactivity**. Coder activity, a new turn, pending
approval, pause, or a changed coder lifecycle resets that window; routine status
notifications and watchdog polls do not. Active turns and subagents prevent this
fallback. Explicit readiness and runtime safety checks remain immediate, and
deliberate review follow-ups and retries keep their existing behavior. For a
native Codex coder, Bello first confirms the idle state with a metadata-only
status read of the owned thread; an unknown or failed read never counts as idle
and is retried on the next 60-second guard tick without restarting the window.

[Smart Execution](async-tools.md) is on by default for new configurations.
[No Need For Context](no-need-for-context.md) is off by default; it summarizes
older agent context while keeping the original task and a recent tail, and never
applies to the runtime supervisor.

**Goal** (`goal`, off by default) keeps a run working toward the original task.
While it is on, completion review is on regardless of its saved value, runtime
supervision follows Goal's own `goal_runtime` switch (default on) instead of
`runtime_enabled`, and completion returns are unlimited both before and after
the optional adversary; the adversary's instructions and pass count are
unchanged. The reviewer is told to check the original objectives and return work
only for a concrete unmet requirement (for example 65/100 where 70/100 is
required), not for new requirements or polish. Bello adds no score parser or
automatic gate. The coder is asked to keep a short `GOAL_MAP.md` of approaches
tried, observed results and evidence, and untried ideas, and to repeat a failed
approach only after a recorded relevant change; reads of that file bypass
log-distiller shortening like the task file. Smart Execution, the revision coder,
No Need For Context and the distiller keep their own switches. Unlimited returns
do not guarantee success; interruption and failure handling still apply.

In Goal mode a successful coder turn that ends without the readiness marker is
continued immediately with a short "continue toward the original Goal" message;
interrupted or failed turns, pending approvals and active subagents are not
continued this way. Completion review starts only after the exact readiness
marker, never because the coder was idle.

`/pause` interrupts the coder and cancels a review in progress; `/resume` continues
the coder at once. If a Goal coder is idle with nothing pending (no approval,
subagent, supervisor check or queued review) for the 40-minute idle window, for
example after a recovered connection, Bello sends one continuation message. Goal
still never starts a completion review because of idle time; only the readiness
marker does.

`goal_runtime: false` (`--no-goal-runtime`, `goal-runtime` in `bello config`)
runs Goal with runtime supervision off: no runtime agent, cheap-triage, runtime
self-test or Goal progress review model calls and no Goal advisories. It uses
the runtime-off mode described below, including its network access and reduced
protection. Automatic continuation, mandatory completion review and the
adversary checkpoint are unchanged. Bello prints a notice when a Goal run uses a
saved `goal_runtime: false`. Outside Goal the switch has no effect.

Runtime reviews use the available chronology and read-only evidence without
requiring file attestations or exact event references before delivering feedback;
runtime finding hashes are not collected or retained. If a review takes at least
60 seconds or an observed code edit occurs after its snapshot, feedback carries
an informational staleness note. The note does not block delivery, pause the
coder, or require a repeat review; the coder checks whether the feedback still
applies. Approval boundaries and protected artifacts are unchanged. Independent
work may continue while an isolated version is being evaluated, with each result
kept tied to the version actually checked.

In Goal mode, runtime may also return `noop` with a short optional advisory,
including up to 2-3 strategic ideas. The coder may ignore it; it is not an
intervention, standing constraint, or reason to restart, and a flat score alone
does not require a change of approach. The same advice keeps a stable
`advisory_key`, deduplicated while that key remains in the bounded advice history.
Different advisories have a 30-minute delivery cooldown.

If a periodic progress review fails (a timeout, a rate limit, a decision that fails
validation, or a lost login), Bello notes the skipped review in `PROGRESS.md` and the
run continues; a lost login still stops the run at the next required check. When a
progress review delivers its optional advisory while a readiness review is queued,
that queued review is cancelled; the coder declares readiness again after the
advisory turn.

An optional `goal_adversary_after_returns` checkpoint (default `0`, disabled)
can start the first adversary before completion has accepted. With Goal and
adversary enabled, `3` means three completion returns and their coder repairs,
then the first adversary at the next readiness; an earlier accept starts it
earlier. After the adversary, completion returns stay unlimited until a fresh
accept, including when the adversary reports no findings. A failed adversary is
not treated as successful completion of this sequence. The configured adversary
pass count is unchanged. The option is dormant outside Goal or with adversary
off, so existing Goal and ordinary review schedules are unchanged by default.

The persisted fields include:

```json
{
  "goal": false,
  "goal_adversary_after_returns": 0,
  "goal_runtime": true,
  "runtime_enabled": true,
  "completion_review": false,
  "adversary": true,
  "async_tools": true,
  "no_need_for_context": {"enabled": false, "threshold_tokens": 244800, "retain_tokens": 24800},
  "log_distiller": {"enabled": false, "model_path": null}
}
```

Run flags select settings for a run:

```sh
bello --goal
bello --goal --adversary --adversary-runs 1 --goal-adversary-after-returns 3
bello --goal --no-goal-runtime
bello --no-runtime --completion-review false --adversary
bello --runtime --log-distiller --no-async-tools
bello --log-distiller --distiller-model /absolute/path/local-bundle
bello --no-log-distiller
```

Review and model flags apply to one run. The switch flags (`--goal`,
`--goal-runtime`, `--runtime`, `--async-tools`, `--no-need-for-context` with its threshold options,
and `--log-distiller`) are also recorded in `.supervisor/config.json`, because
recovery resumes a run with the switches it started with; later runs use them
until changed. Goal keeps the saved runtime choice, so turning Goal off restores
it.
The Goal adversary checkpoint is also recorded for recovery. Omit its CLI option
to preserve the saved value, or use `--goal-adversary-after-returns 0` to disable it.

With runtime supervision off (`runtime_enabled: false`, or `goal_runtime: false`
in Goal), cheap runtime triage is effectively off too, and the editor hides the
runtime model, effort and cheap-triage rows. This mode
enables network access inside each role's existing filesystem sandbox, allowing
dependency installation into the writable workspace or temporary directory.
It reduces safety: runtime model review is absent and commands can send readable
data over the network. Filesystem boundaries, protected files, the coder's
completion-marker protocol and independently selected completion/adversary
reviews remain in force. Runtime validation/readiness gates do not run.
Outside-sandbox escalation is unavailable in this mode. The final patch skips
the name-based guesses for secret-like files and grading/hidden-looking paths;
declared protected paths and controller/Git state stay protected.

Cheap triage uses `gpt-5.6-luna` at `medium` effort through the Codex login by
default. `BELLO_RUNTIME_TRIAGE_MODEL` overrides the model and
`BELLO_RUNTIME_TRIAGE_EFFORT` its effort; the selected backend validates an
explicit effort against the model's advertised capabilities.

Runtime reviews label earlier inspections as historical, stale (a matching file
was edited later) or unknown instead of treating old reads as current; a
suspected defect from an earlier observation is checked against current
evidence before it is reported. `persistent_decision` is reserved for explicit
user constraints, authorization boundaries and established task requirements;
temporary diagnostics, preferred tactics and Goal advice never become standing
constraints. Restart requests need stronger evidence than ordinary feedback.

Resume preserves a thread's tool contract. Changing runtime or distiller switches
requires a fresh run if saved threads use a different contract; Bello reports
the mismatch instead of silently continuing with old tools. An unchanged run
keeps distillation across recovery and subsequent coder turns.

## Tools, subagents and recovery

Pi's built-in tools and ambient extensions are disabled. Claude Code exposes
only Bello's managed MCP tools. Both engines send commands, filesystem actions
and delegation requests through the same Bello tool host. The model process
cannot approve its own command or select a different filesystem scope.

Contained operations execute inside the role's OS sandbox. An explicit
escalation request passes through Bello's existing approval handler; read-only
roles with approvals disabled cannot escalate. An accepted escalation applies
to that command, not subsequent commands or child agents.

Long commands can yield a managed session handle, then be polled or stopped
without starting another process. Sessions are owned by the originating thread
and turn and are cleaned up before that turn finishes. Interactive stdin and
PTYs are not exposed by these tools.

Command output, including polling and stopping command sessions, uses a default
and maximum budget of approximately 10,000 tokens. This is a size estimate of
four UTF-8 bytes per token, giving a 40,000-byte body ceiling; it is not a model
tokenizer count. A tool may request a smaller `max_output_tokens`. Truncation
preserves the beginning and end, marks the omitted middle, and respects UTF-8
boundaries. The marker and result metadata sit outside the body budget.

File reads, searches and directory listings keep their separate limit of 2000
lines or 50 KiB, preserving the head. File reads provide continuation offsets;
a partly displayed line is reread on continuation. These ordinary limits are
mechanical truncation. The controller retains separate, larger captured evidence
for validation; that capture and command-session buffers also have finite limits.

A test command still counts as validation evidence when it redirects with `2>&1`
(also followed by `; echo exit=$?` or piped through `tail`). Commands that write
their output with `&>file` or `>&file`, or run tests in the background, are not
counted.

A coder using one provider can request a child using another provider, provided
the profile is allowed and both accounts are configured. Children retain the
parent workspace boundary. Coder delegation follows the configured concurrency
limit; reviewer delegation remains one level deep. Built-in unmanaged provider
subagents are not used.

Sessions and tool dispatch records live in private controller state, outside the
agent workspace. Stable tool-call identities prevent a redelivered request from
executing twice. After a lost connection, an uncertain command is reported as
uncertain and is not automatically replayed. This does not claim transactional
rollback of external actions an approved command may already have performed.

Cancellation fences further tool requests and stops the managed work. On macOS,
a deliberately detached `setsid`/double-fork process can outlive process-group
cleanup while remaining sandboxed, as in the previous Codex execution path.
Do not treat process-group cleanup as a guarantee that every deliberately
daemonized process has exited.

## Optional local log distiller

Install the additional inference dependencies only when using the distiller:

```sh
python -m pip install -e '.[log-distiller]'
```

Use `.[claude,log-distiller]` if both optional backends are needed. This installs
code dependencies. When distillation is enabled and `log_distiller.model_path`
is `null`, Bello downloads its default model once into the Hugging Face cache
and reuses the cached files, including offline. With distillation off, it does
not download or load the model. The distiller runs locally in a separate worker;
it does not send logs to Hugging Face or add a paid model call.

The default is [Makson179/bello-log-distiller](https://huggingface.co/Makson179/bello-log-distiller),
pinned to revision `436bf8dceecb30d5494519d21175bc03e5c98795`, about 599 MB.
No Hugging Face account or token is required. To download it before a run:

```sh
python -m supervisor.runtime.distiller_download
```

Model files are separate from the Python package. The model is distributed under
Apache 2.0 with its upstream notice; Bello's code remains MIT. Datasets and private
logs are not part of the download. A missing dependency, failed download or
invalid bundle metadata is reported before model work, not treated as permission to
disable distillation or change providers silently. Native Codex additionally
requires the compatible selection-hook executable described above.

The supported model bundle contains a trained fresh ModernBERT-base encoder with
the 768→256→1 token MLP, its matching original tokenizer/configuration assets,
checksums and inference recipe. An older R12 model with line and token heads is
not interchangeable. `--distiller-model PATH` or `log_distiller.model_path`
overrides the default with an existing compatible local bundle; relative paths
resolve from the project root. No default model is downloaded for this override.
For development, given an existing matching checkpoint and asset folder, create
a new local bundle:

```sh
python -m supervisor.runtime.distiller_bundle \
  --checkpoint /absolute/path/fresh-checkpoint.pt \
  --assets /absolute/path/original-assets \
  --output /absolute/path/new-distiller-bundle
```

The exporter hashes the files and creates links to them, without copying or
loading the large checkpoint. The destination must be new and its source files
must remain available. `--cutoff FLOAT` optionally records a chosen trained
operating point; the default is the fresh selector's saved R80 cutoff
`-0.47521790862083435`. The bundle recipe carries the cutoff, rather than a
separate run setting. Set `log_distiller.model_path` to the resulting folder.
An enabled run requires a valid bundle. Preflight checks metadata, file presence
and availability of the optional inference packages without importing them. The
worker checks hashes and model tensors when it first loads them.

Distillation applies to model-facing text tools throughout the coder lifecycle,
including resumed/revision coder sessions and coder children. Pi and Claude Code
use the common tool host; native Codex uses the verified selection hook above.
Runtime, completion and adversary reviewers do not receive it. Eligible coder
tools expose a short `focus` field, with a 120-character maximum, and a brief
instruction asks the coder to supply it. Without a nonempty focus the ordinary
output is returned.

The configured task file and explicit reads of named TASK/README/AGENTS/CLAUDE/
INSTRUCTIONS/SPEC/SPECIFICATION documents bypass the selector, as do recognised
CLI help requests. This is a narrow list, not an exclusion of all Markdown,
documentation directories, JSON, test output or diffs. In a recognised mixed
command the entire ordinary reply is retained; later polls/stops keep the
original command and working-directory context. The check recognises common
shell reads and literal Python file/help calls, not arbitrary dynamically
constructed programs or semantic importance. There is no extra model call or
prompt instruction. Normal capture, size limits and file pagination still apply.

The selector receives output **after the ordinary output budget**. It keeps
original excerpts from that bounded text and cannot recover text already omitted
by capture, buffering or truncation. Command status, exit code and session handle
remain intact; controller evidence is unchanged. ModernBERT processes the received
text in 8192-token windows, including focus/command conditioning, with 256-token
overlap. Longer input is covered across windows rather than cut at one window.

The internal worker deadline is 300 seconds. Worker errors or timeouts retain the
ordinary bounded output; an interrupted call is cancelled without replaying its
command. These behaviors do not establish benchmark quality, recall or savings.

## Front ends

Codex and Claude Code plugins invoke the same installed Bello binary; the front
end does not determine which provider must perform the task. Delegation and
configuration-advisor sources are in [plugins/bello](../plugins/bello). The
advisor is migrated: it reads Bello's available provider/model/effort catalog
and recommends one concrete setup. Marketplace publication and installation of
the updated plugin remain separate release steps.

Pi's MIT notice ships with its bridge in `supervisor/pi_worker`. The official
Claude SDK and its CLI remain separate dependencies under their own terms.
