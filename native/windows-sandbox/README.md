# Bello native Windows sandbox

This binary is a security boundary for restricted commands on 64-bit Windows.
It launches the fixed system `cmd.exe` inside a unique less-privileged
AppContainer (LPAC), atomically attaches it to a kill-on-close Job Object, and
temporarily grants the AppContainer SID access only to the requested roots.

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
