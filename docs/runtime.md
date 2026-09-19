# Multi-provider runtime

This document describes the `0.6.0.dev0` development branch. It is not an
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
Select the tested Codex 0.153.4 build with `BELLO_CODEX_BINARY`; its binary hash is
pinned in Bello. An alternate compatible build requires an explicitly trusted
local `BELLO_CODEX_SELECTION_MANIFEST`. Bello verifies the hash, selection
protocol and bridge deadline before use. The bridge allows at least 315 seconds
around the selector's 300-second limit. Native distillation is Unix-only; this
build was tested on Linux, not macOS. The patched executable is not bundled or
installed automatically; see [native selection setup](native-codex-selection.md).
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

The Claude extra includes the pinned official Agent SDK and its official CLI.
It can be omitted when no role uses `claude-code`. Pi's exact dependency versions
are installed from the bundled npm lockfile; the Node dependency tree is not
copied into the Python wheel. `BELLO_NODE` can select an existing compatible
Node executable. `BELLO_RUNTIME_DIR` can select the private Pi installation
cache; `BELLO_PI_AGENT_DIR` selects the user's Pi authentication/config directory.

Linux restricted execution requires `bwrap` (the `bubblewrap` package) and
working unprivileged user namespaces. macOS uses the operating system's Seatbelt
sandbox. Native Windows uses Bello's packaged LPAC helper. Windows source builds
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

The Claude subscription route rejects environment variables that redirect it to
an API key, alternative endpoint or cloud provider. It does not silently clear
those variables or fall back to pay-per-token API usage. Use an explicitly
selected Pi API provider when API billing is intended.

The editor shows models and reasoning efforts reported by the authenticated
engines. Preflight validates every active role and allowed child profile before
starting model work. Unsupported efforts or Fast mode are errors, not silent
substitutions. In particular, `ultra` is not treated as a synonym for `max`.

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

The persisted fields are `runtime_enabled`, `completion_review`, `adversary` and
the nested `log_distiller` object:

```json
{
  "runtime_enabled": true,
  "completion_review": false,
  "adversary": true,
  "log_distiller": {"enabled": false, "model_path": null}
}
```

Run overrides do not change the saved project configuration:

```sh
bello --no-runtime --completion-review false --adversary
bello --runtime --log-distiller
bello --log-distiller --distiller-model /absolute/path/local-bundle
bello --no-log-distiller
```

With runtime supervision off, cheap runtime triage is effectively off too, and
the editor hides the runtime model, effort and cheap-triage rows. This mode
enables network access inside each role's existing filesystem sandbox, allowing
dependency installation into the writable workspace or temporary directory.
It reduces safety: runtime model review is absent and commands can send readable
data over the network. Filesystem boundaries, protected files, the coder's
completion-marker protocol and independently selected completion/adversary
reviews remain in force. Runtime validation/readiness gates do not run.
Outside-sandbox escalation is unavailable in this mode.

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
