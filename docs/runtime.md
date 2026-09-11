# Multi-provider runtime

This document describes the `0.6.0.dev0` development branch. It is not an
announcement of a published release. Platform and live-account verification is
tracked in [the implementation record](implementation-0.6.0.md).

## One Bello pipeline, two execution paths

Bello still owns the coder, runtime supervision, completion review, adversarial
testing, revision coder, private plans, disposable workspaces and final patch.
An execution engine supplies the model conversation; it does not decide Bello's
approvals or completion gates.

| Model selection | Engine | Authentication |
| --- | --- | --- |
| `openai-codex/<model>` | Pi | The user's own OpenAI subscription login through Pi |
| `openai/<model>` | Pi | OpenAI API credentials |
| `anthropic/<model>` | Pi | Anthropic API credentials |
| Other Pi `provider/model` identifiers | Pi | That provider's supported authentication method |
| `claude-code/<model>` | Official Claude Code through its Agent SDK | The user's own Claude subscription login |

Existing unqualified `gpt-*` selections keep the `openai-codex` subscription
route. They do not become API calls. Bello never changes the provider or billing
route to work around a missing login or unavailable model. Provider-specific
limits and billing rules still apply.

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

This grants only attributes, extended attributes, permission-descriptor reads
and synchronization on those two directories. It does not grant listing,
file-content reads, writes, or inherited access to descendants. The permission
persists until explicitly removed with `bello runtime windows-sandbox remove`
from an administrator terminal; stop runs before removal. Existing unrelated
permissions are preserved. The named capability is not an authentication check:
another process can request the same capability. Its access remains limited to
this fixed metadata permission, rather than applying to all AppContainers.

Preparation does not grant access to arbitrary drives or protected machine-wide
toolchains. Exposed toolchain roots must still allow the invoking account to
manage their exact permissions; use host-controlled per-user installations.
Other required parent directories receive temporary metadata-only permissions
for the individual command, when the invoking account owns them. These do not
allow listing or access to sibling files and are removed during cleanup.
Administrators-owned parents require an already elevated caller; unsupported
locations are rejected rather than triggering automatic elevation.

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

Model-facing text uses the ordinary Pi-sized limit of 2000 lines or 50 KiB:
command output keeps the tail, file reads keep the head and provide continuation
offsets. Truncation is explicitly marked. This is a mechanical output limit,
not learned or semantic compression. The controller retains the separate,
larger captured evidence for its existing validation logic.

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

## Front ends

Codex and Claude Code plugins invoke the same installed Bello binary; the front
end does not determine which provider must perform the task. Delegation package
sources are in [plugins/bello](../plugins/bello). Configuration-advisor migration
is a separate follow-up, not an automatically replaced skill in this branch.

Pi's MIT notice ships with its bridge in `supervisor/pi_worker`. The official
Claude SDK and its CLI remain separate dependencies under their own terms.
