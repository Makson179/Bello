# Bello native Windows sandbox

This binary is a security boundary for restricted commands on 64-bit Windows.
It launches the fixed system `cmd.exe` inside a unique less-privileged
AppContainer (LPAC), atomically attaches it to a kill-on-close Job Object, and
temporarily grants the AppContainer SID access only to the requested roots.

## System-directory preparation

The separate fixed-argument commands `host-status`, `host-prepare`, and
`host-remove` operate only on non-inherited metadata permissions for two fixed
OS directories: the system-drive root and the machine-configured profiles directory. They
never enter command execution or recovery and accept no paths, commands, SIDs,
or permission masks. Status is read-only. Prepare and remove require an
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

Journal v4 stores these ancestor grants separately from recursive authorities.
Recovery validates their identities and revokes the exact per-run entry without
walking or deleting the ancestor tree. Existing v3 journals remain readable
without ancestor records.

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
explicit recovery request only after the failed helper has exited (and its Job
handles have therefore closed), while every normal path proves the Job is empty
before revocation. This intentionally prefers a temporary access residue for a
dead, unguessable AppContainer SID over revoking permissions while a descendant
could still be alive.

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
