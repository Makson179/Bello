# Bello native Windows sandbox

This binary is a security boundary for restricted commands on 64-bit Windows.
It launches the fixed system `cmd.exe` inside a unique less-privileged
AppContainer (LPAC), atomically attaches it to a kill-on-close Job Object, and
temporarily grants the AppContainer SID access only to the requested roots.

## System-directory preparation

The separate commands `host-status`, `host-prepare`, and `host-remove` operate
only on non-inherited metadata permissions. Without `--drive`, they select two
fixed OS directories: the system-drive root and machine-configured profiles
directory. They never enter command execution or recovery and accept no
directory paths, commands, SIDs or permission masks. Status is read-only. Prepare and remove require an
administrator terminal; the helper does not elevate itself. The user-facing
commands are `bello runtime windows-sandbox status|prepare|remove`.

Both directories are obtained from Windows, not environment variables. The
permission recipient is the capability `Bello.Sandbox.SystemRootMetadata.v1`,
also present in Bello's command tokens. Its fixed mask `0x00120088` permits
attributes, extended attributes, permission-descriptor reads and synchronization.
It does not permit directory listing, content access, writes, or inheritance.
This is a persistent machine-level permission, not part of per-command cleanup.
It does not grant rights to AAP/ARAP as a whole. The capability name is not an
unforgeable application identity: another launcher may request it, so its narrow
rights, not secrecy, are the boundary.

Setup preserves unrelated ACEs and refuses conflicting entries for the same
capability. Removal targets only the exact fixed permission, never a saved
whole-drive ACL. Stop runs before removal. The setup lock serializes Bello's
administrative changes; it cannot serialize unrelated administrators editing
the same ACL. Do not run independent ACL-management tools concurrently.

Preparing these two directories does not authorize metadata on other drive roots or
provide access to protected toolchain directories. Native tests must still prove
real CMD/Node operation and private-file denial after preparation.

For a workspace or toolchain on another fixed local drive, explicit preparation
accepts `host-status --drive D:`, `host-prepare --drive D:` and
`host-remove --drive D:`. This selects only that drive root, with the same exact
non-inheriting metadata capability. It does not prepare other drives or accept
directory paths, custom SIDs, masks or commands. The helper rejects network,
substituted and non-fixed drives and pins the selected root before mutation.
Preparation/removal still require an administrator terminal; no run elevates.

The mutually exclusive selector `--null-device` instead operates only on the
fixed NT device `\Device\Null`. Its separate capability
`Bello.Sandbox.NullDevice.v1` receives non-inherited `0x0012019f` read/write
access, without execute, delete, ownership or ACL-management rights. NUL supplies
EOF and discards output; standard Python test capture requires it. Device setup
does not grant access to files or other devices and preserves unrelated ACEs,
owner, group and integrity label. Windows resets its descriptor on reboot:
check `host-status --null-device` again afterwards and explicitly prepare it
from an administrator terminal if needed. This selector installs no service or
scheduled task and does not elevate automatically. `host-remove --null-device` removes only the exact
named permission. The capability's narrow rights, not secrecy of its name,
define the access granted.

## Offline networking

The separate, mutually exclusive `--network` selector prepares one fixed
`BelloOfflineNetwork` Windows service. It requires explicit administrator
approval and installs this helper under the OS Program Files directory, with
an administrative owner and protected permissions. The service runs as
LocalSystem and starts with Windows; agent commands remain unprivileged LPAC
processes. There is no general elevated command or firewall-rule interface.

An offline command is created suspended. Before it resumes, the service checks
the real local caller, the child's AppContainer identity and its Job Object,
then installs four Windows Filtering Platform BLOCK rules for that exact
package SID: IPv4/IPv6 connect and receive/accept. The token can create a socket
(needed even by Python imports), but the rules block direct TCP/UDP traffic.
If service setup or rule verification fails, the command does not run. Online
commands do not use these blocking leases.

System DNS resolution through the Windows DNS Client (`Dnscache`) service is
not fenced by these per-package rules. Native Server 2025 tests confirm that
the service can send a query on an offline LPAC process's behalf. Query names
can disclose domains or carry encoded data that the command is allowed to read.
This is an explicitly accepted Windows 0.6.0 limitation, not a fixed issue:
offline mode blocks direct connections but does not guarantee complete network
isolation or prevent DNS-based data exfiltration. Tests retain direct-traffic
denial checks and positive controls while characterizing this DNS behavior.

Rules persist if the service crashes. Normal release requires the process Job
to be empty; uncertain same-boot leases remain blocked. A verified new boot
allows stale lease cleanup. Service removal or replacement refuses active or
retained leases, and never removes unrelated firewall objects. Inspect with
`bello runtime windows-sandbox status --network`; explicitly prepare/remove
with the matching command in an administrator terminal.

For other strict ancestors of allowed roots, the helper checks the actual child
token and temporarily adds only missing metadata access for the unique per-run
SID. The directories and their ancestry are pinned before mutation. Windows
must allow the invoking account to open each exact object with `WRITE_DAC`;
the helper never takes ownership or elevates itself. The owner's SID alone is
not an access check: a user can legitimately control permissions on a directory
owned by an administrator or the system. Permissions never inherit or permit
listing or sibling content access.

All file-ACL read/modify/write/readback operations, including authority grants,
ancestor metadata and revocation, use one cross-session synchronization mutex.
Commands execute outside that lock. Authenticated users can synchronize on the
mutex and inspect its fixed descriptor, but this grants no file permissions.
Another local process can cause a bounded timeout by holding it; the helper then
fails closed. This cooperative lock cannot serialize unrelated ACL-management
tools that do not participate in the protocol.

Journal v5 stores these ancestor grants separately from recursive authorities
and records whether crash recovery needs the network broker's empty-Job proof.
Recovery validates their identities and revokes the exact per-run entry without
walking or deleting the ancestor tree. Existing v3/v4 journals remain readable;
they do not require a broker that did not participate in those older runs.

## Command protocol and cleanup

The controller sends one little-endian `u32` length followed by a UTF-8 JSON
request and deliberately keeps stdin open. EOF is a parent-death signal: the
helper terminates its Job Object, so a controller crash or cancellation cannot
leave a descendant process tree running. Child stdout and stderr are merged as
raw bytes on helper stdout. Helper stderr contains exactly one terminal JSON
record and is never shared with the child.

ACL mutations are journaled before application. Normal cleanup and the next
helper invocation revoke every ACE for the unique per-run SID before deleting
the AppContainer profile. Existing reparse points, hard links, null DACLs,
remote/device paths, and filesystems without persistent ACLs are rejected
rather than handled with a weaker fallback.

Unwinding or dropping a journal never changes ACLs: an exceptional path leaves
the durable record and unique profile in place. The controller issues an
explicit recovery request only after the failed helper has exited. For an
offline run, the broker can still hold the Job, so recovery also requires its
confirmation that the original Job is empty. Every normal path likewise proves
the Job is empty before revocation. If that cannot be proved, the journal,
permissions and blocking rules remain for later recovery.

The helper must be built and exercised on native Windows. A non-Windows Cargo
build validates only the protocol layer; it is not evidence that the Win32
boundary works.

The current one-shot design validates and verifies every object below each
authority before launch and again as needed for ACL cleanup. Its setup and
recovery cost is therefore O(number of filesystem objects), with a fail-closed
500,000-object ceiling. Native CI records representative large-tree timings
without treating an arbitrary wall-clock threshold as a security assertion.

Every granted root must reside on a local persistent-ACL filesystem and must
allow the invoking account to read and temporarily modify its DACL. In
particular, a standard user normally cannot grant AppContainer access directly
to protected machine-wide toolchains under Program Files. Callers must expose
an exact host-controlled per-user runtime/toolchain root; the helper never
broadens access or falls back to an unsandboxed process.
