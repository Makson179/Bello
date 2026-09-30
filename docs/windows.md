# Native Windows installation and troubleshooting

## Supported baseline

Bello supports native 64-bit Windows 11 and Windows Server 2022/2025 with
Python 3.11 or newer. CI runs the complete applicable suite on Windows Server
2022 with Python 3.11 and Windows Server 2025 with Python 3.14. Both jobs run
`bello --help`, native sandbox checks and the Pi integration with a local test
provider, and record the exact GitHub runner image.
Windows on ARM, Windows 10, older Windows Server releases, FAT/exFAT
workspaces, and remote filesystem providers are not claimed as supported
because they are not in that matrix.

WSL2 is also supported as Linux, but it is separate from native Windows
support. Current Codex CLI releases do not support WSL1. Use either an
all-native toolchain or an all-WSL2 toolchain for one run.

## Prerequisites

Install each prerequisite as a native 64-bit Windows application and make sure
it is visible in a new PowerShell or Command Prompt session:

1. Python 3.11 or newer (`py -3 --version`).
2. Git for Windows (`git --version`).
3. Node.js 24 or newer (`node --version`) for the Pi runtime.

Then install Bello without elevation:

```powershell
py -m pip install --user pipx
py -m pipx ensurepath
pipx install bello
bello runtime install
bello runtime login openai-codex
bello doctor
```

Task execution does not need Administrator access or Windows Developer Mode.
Host sandbox preparation does require an explicit administrator terminal:

```powershell
bello runtime windows-sandbox prepare
bello runtime windows-sandbox prepare --null-device
bello runtime windows-sandbox prepare --network
```

The first command grants only metadata access on fixed system directories.
The second permits access to NUL, which Python test capture needs; Windows may
reset that device permission on reboot. The third installs the fixed
`BelloOfflineNetwork` service in Program Files. It starts with Windows and runs
as LocalSystem only to manage blocking rules for sandbox processes. Agent
commands do not run as administrator. Nothing elevates silently.

Use the corresponding `status` commands to inspect setup without changing it.
For a project or toolchain on another local drive, prepare only that drive's
root with `bello runtime windows-sandbox prepare --drive D:`. Offline commands
refuse to start if their network blocking rules cannot be established.

### Claude Code subscription on native Windows

Bello 0.7.1 pins `claude-agent-sdk==0.2.161` on every platform. That SDK
release has no Windows wheel with a bundled Claude Code CLI, so on Windows pip
installs its pure-Python source distribution, and Bello supplies the identical
official Claude Code 2.1.284 build that the SDK's macOS and Linux wheels bundle:

```powershell
pipx install 'bello[claude]' --force
bello runtime install claude-code
bello runtime login claude-code
bello doctor
```

`bello runtime install claude-code` downloads `claude.exe` once from
`https://downloads.claude.ai/claude-code-releases/2.1.284/win32-x64/`, checks the
exact size and SHA-256 pinned in Bello, and keeps it in a private,
ACL-checked cache under `%USERPROFILE%\.bello\runtime\claude-code` (or
`BELLO_RUNTIME_DIR`, which must be private to you, SYSTEM and Administrators
inside a directory that gives other accounts at most read access; a shared
folder such as a CI runner's temp directory is refused with the rejected path
and ACL entry, and its permissions are never changed).
`bello update` and the start of a run whose roles use
`claude-code/...` prepare it the same way; `bello doctor`, `bello config` and
backend startup only verify the local file. Bello never uses a `claude` from
PATH or the project, a different CLI version, or an API route instead. A failed
or tampered download stops with an actionable message; nothing is replaced
automatically. The download needs network access to `downloads.claude.ai`
(unavailable in some regions); no login or model request is involved.

## Native use

Open PowerShell or Command Prompt in a project on local NTFS, then run:

```powershell
bello doctor
bello --task .\TASK.md
```

The interactive configuration editor is available through `bello config`.
Commands proposed by the coder may use PowerShell or `cmd.exe`; ambiguous shell
syntax is reviewed or denied rather than auto-approved.

## Filesystem locations

- Prefer a short path on a local NTFS volume, such as `C:\src\project`.
- A project, temporary directory, and isolated runtime home may be on different
  local volumes. Bello copies across volumes and does not create cross-volume
  hardlinks.
- Drive-letter and UNC syntax is recognized. UNC/network shares and cloud-sync
  folders are not in CI and can expose provider-specific reparse behavior; move
  to local NTFS when a safety check rejects one.
- Do not use Windows reserved device components such as `CON`, `NUL`, `COM1`,
  or names ending in a dot or space. Alternate data stream syntax is rejected
  for workspace changes.
- Bello refuses a junction, symlink, hardlink, or other reparse arrangement
  when it cannot prove that snapshot, rollback, and protected-path boundaries
  remain intact. Internal links in `.venv`, `venv`, and `node_modules` are
  materialized as regular isolated content only when their targets remain
  inside the project; external targets are rejected.
- Linked Git worktrees are not currently supported because their `.git` entry
  points outside the project boundary. Use a regular clone for a Bello run.

On native Windows, the isolated task copy is held with a non-write,
non-delete-sharing handle. Read-only dependency copies are watched with
`ReadDirectoryChangesW`, so a modify-test-restore sequence in one command is
still treated as an integrity failure instead of becoming trusted validation.

## WSL2 alternative

Inside WSL2, install the Linux builds of Python, Git, Node.js, Bubblewrap, and Bello and keep
the project under the distribution's Linux filesystem, for example:

```bash
cd ~/src/project
pipx install bello
bello runtime install
bello runtime login openai-codex
bello doctor
bello --task TASK.md
```

Avoid mixing native executables with `\\wsl$` paths. `/mnt/c` works for many
Linux tools but has different metadata and substantially slower small-file I/O;
the WSL home filesystem is the recommended location.

## Troubleshooting

### `python`, `git`, `node`, or `bello` is not found

Close and reopen the terminal after installation. Run `py -m pipx ensurepath`
again for a pipx install. `bello doctor` prints the exact executable path it
found for the runtime and Git, so a stale or mixed WSL/native PATH is visible.

### Runtime installation or authentication fails

Run `node --version`, `bello runtime install`, and the login command for the
selected provider in the same terminal. Then rerun `bello doctor`.
`bello runtime install pi` and `bello runtime install claude-code` prepare one
component at a time; the Claude Code CLI does not need Node.js.

### `bello config` shows a provider as not connected

The STATUS panel lists each engine that could not report models, with a short
reason and the next command (for example, Claude Code not signed in, the
official CLI not prepared, or an API-key variable that would switch Claude
Code away from the subscription route). Details from providers are not shown.

### Offline network service is missing or outdated

Check `bello runtime windows-sandbox status --network`. Explicitly run
`bello runtime windows-sandbox prepare --network` as administrator when setup
or a service binary update is needed. Updates and removal refuse active runs
and retained leases; finish runs first. A service crash leaves blocking rules
in place. Uncertain leases survive until a verified Windows restart, rather
than removing protection from a process that might still be running.

### Snapshot creation reports a reparse, hardlink, or path-safety failure

Move the repository to a plain local NTFS directory and remove directory
junctions or links that point outside it. Do not run the terminal as
Administrator to make the check disappear: the refusal protects the original
workspace and rollback boundary.

### Access denied or path too long

Stop programs that hold files in the project or isolated temporary directory,
and retry from a shorter project path such as `C:\src\project`. Antivirus and
sync clients can transiently lock files; use a local nonsynchronized directory
for the run. Bello reports cleanup failures rather than silently leaving a
process tree or snapshot behind.

### A PowerShell or Command Prompt command is sent for review

This is expected when quoting, interpolation, redirection, a script block, or
command composition is ambiguous. Bello keeps the existing fail-closed approval
policy; rewrite the command as a simple argv invocation when possible.
