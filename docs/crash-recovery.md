# Controller crash recovery

Recovery continues one logical Bello run; it is not a fresh attempt at the task
and it is not a replay of the last shell command. It preserves the trusted
working snapshot, coder session, effective configuration, event history and
already-consumed review/restart budgets.

## Process lifecycle

Normal console and `python -m supervisor.main` launches enable the process
watchdog by default. `--no-auto-recover` disables automatic process restarting;
it does not authorize discarding an unfinished run's state. Python callers that
invoke the Click command with an explicit argument list keep the in-process API.

A separate guardian owns the worker's process lifetime. After an unexpected
worker exit, the watchdog may restart it only if both checks succeed:

1. The guardian proves that the previous worker and its descendants have stopped.
2. The durable controller record describes a safe, internally consistent
   continuation boundary with no uncertain actions.

At most three automatic restarts are reserved durably for each logical run,
with delays of one, two and four seconds. Starting another monitor does not
replenish that budget. Completion, escalation, provider/authentication failures,
blocked recovery and human interruption do not trigger an automatic retry.
Terminal input and Ctrl-C still reach the worker; interruption allows a bounded
graceful shutdown before remaining owned processes are fenced.

## Platform boundary

| Host | Descendant fencing for automatic recovery |
| --- | --- |
| Linux | Requires subreaper support and pidfd-based ownership; unsupported or unsuccessful cleanup blocks recovery. |
| Windows | Uses an inherited, non-breakaway Job Object, assigned before the worker starts executing. |
| macOS | Process groups cannot prove cleanup of arbitrary detached native descendants. Production automatic continuation therefore fails closed after a crash. |

The macOS limitation is deliberate: a surviving command must not race a newly
started controller. Local process-group fixtures do not qualify arbitrary native
descendant cleanup. Filesystem sandboxing remains active, but it is not proof
that every old process has exited.

The watchdog does not install a system service or configure startup after an OS
reboot. A machine reboot or the loss of the guardian is not permission to trust
PIDs saved on disk.

## What is safe to continue

The controller validates the same task and private plan, model/configuration,
sandbox, runtime journal, session identity, state-file hashes and consumed
counters. Snapshot restoration additionally validates directory identities,
trusted task bytes, Git baseline contents, dependency roots and protected runtime
mounts. Native Windows guards are reopened only after validation.

The effective system-prompt file, including a custom override, and the selected
engine executable/manifest are bound to the checkpoint. A different selection is
rejected before starting that engine or rewriting restored state. Full-access
coder runs remain usable, but cannot automatically recover: their tools could
modify the controller's recovery authority. Unknown script launchers that delegate
to an unbound executable likewise remain nonrecoverable.

An interrupted coder can continue from a known checkpoint after its provider
history is reconciled. A command that completed after the checkpoint but whose
result was not observed is not silently adopted as reviewed evidence. Pending
approvals are not replayed. Interrupted controller transitions, active children,
unfinished reviews, missing/corrupt authority and uncertain command outcomes can
require manual inspection instead of automatic continuation.

Final application to the original workspace has its own durable transaction.
An interrupted application is never blindly repeated. A recorded committed
result is reusable only while its output still matches; changed output is a
conflict, not permission to overwrite the user's edits.

## State and manual handling

Controller-owned recovery records live under the original project's protected
`.supervisor/controller/`, outside the coder's writable snapshot. The lifetime
ownership lock is acquired before configuration creation or cleanup; another
controller cannot take over a live run. Legacy diagnostic checkpoints alone are
not sufficient recovery authority.

`--recover` explicitly requests a previously fenced, eligible interrupted run.
It requires a matching guardian receipt and never silently starts a fresh task.
It cannot be combined with `--clean`, `--start-over` or `--no-auto-recover`.
Do not edit recovery records or fabricate fencing receipts to bypass a refusal.
Inspect the saved workspace and diagnostic reason first. An explicit new-run
request is a separate decision, not an automatic fallback after failed recovery.

## Verification

Recovery tests use isolated projects and synthetic providers. They cover process
death, exclusive ownership, corrupt/stale state, consumed budgets, cancellation,
snapshot integrity, and crashes around patch application. Real model smoke tests
exercise normal operation through the same process integration. macOS tests do
not establish Linux process-tree or Windows Job Object behavior; those require
the corresponding native CI runners.
