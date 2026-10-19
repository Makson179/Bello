# Async tools (Smart Execution)

An execution mode for reducing model turns spent waiting for tools.
It does not change the model, billing route, sandbox, approvals, or review stages.

Smart Execution is on by default for new configurations. A configuration that
already saves `"async_tools": false` keeps it off; change it with `bello config`
(**async-tools**) or in the project configuration. `bello --no-async-tools` and
`bello --async-tools` select it for a run; like the other run switches, the
choice is recorded in `.supervisor/config.json` for recovery and later runs.

The setting is fixed for a run and inherited by coder, completion reviewer,
adversary, their subagents, and repair/revision threads. Runtime supervision and
cheap runtime retain their bounded wakeup behavior. Switching an existing
conversation between execution modes is rejected; start a new run instead.

## Execution

The model is instructed to group necessary independent actions, not invent
additional work. Dependent actions and conflicting writes remain sequential.
Execution deadlines and cancellation still apply.

- Pi dispatches independent tool calls concurrently through its agent loop.
- Native Codex uses the corresponding native-loop patch. Non-interactive
  command runs and yielded Code Mode cells are waited for by the program.
  Interactive terminals retain their explicit input/session semantics.
- Claude subscriptions stay on the official Claude Agent SDK. Its opt-in
  `run_parallel_tools` tool launches a batch of ordinary Bello tool calls.
  Late results continue the same SDK session at its response boundary.
  This is not an API-provider replacement or a claim that the closed SDK has
  the same scheduling interface as Pi/native Codex.

The first ready result opens a one-second collection window. Further completions
from the same batch restart that window while it is still open and other siblings
are running. This also applies to late results after an earlier delivery, so
nearby completions of long-running tools can reach the model together.
All-finished batches return immediately. Each batch keeps its own window;
unrelated batches and progress messages do not extend it.

If no result is ready, the program continues waiting; expiry alone does not call
the model. User steering and cancellation interrupt the wait. Ready batches can
be delivered while other work continues. Finishing an answer with pending work
waits for the remaining results instead of completing the Bello turn. Each late
result is delivered once and appended, not substituted into previously sent
history. Existing provider caching stays enabled in both modes; Smart Execution
does not request a one-hour cache.

Long custom commands are held to completion by the tool host. Excess commands
queue within existing concurrency limits. For server checks, start the server,
probe it, and shut it down within a finite command; native interactive terminals
are the explicit exception for work intentionally kept running.
A child wait does not return repeated
“still working” messages in this mode. Repeated waits in a parent turn omit
already-delivered child messages; a new parent turn may replay them to avoid
losing a finding after interrupted delivery.

## Native build

Native Codex must advertise the experimental `bello_async_tools` capability.
The older published selection-only bundles do not contain it. Bello downloads a
compatible helper on Apple Silicon, Windows x86_64 and Linux x86_64 (glibc 2.35+);
elsewhere, point `BELLO_CODEX_BINARY` at a compatible build or turn Smart
Execution off. An incompatible binary is rejected before starting a task;
enabling the flag is not a silent fallback to Pi or a partial native implementation.

For source-build testing, use `BELLO_CODEX_BINARY` to select the compatible
binary. Distillation additionally uses its selection manifest as before.
The global Codex installation and account configuration are not replaced.

## Completion input (separate change)

Completion now starts with its instructions, task, accumulated required behavior,
and a concise evidence index. Detailed validation records, logs, events and prior
findings are separate read-only files outside its writable review copy. It reads
the relevant records rather than receiving the full accumulated history upfront.
Files remain available across review turns and internal retries and are removed
when the review closes. Evidence counts are not an acceptance verdict.

This change is independent of Async tools. Adversary's initial packet is unchanged.
The log distiller remains limited to the coder and its subagents; it is not enabled
for reviewers by either change.

## Verification

Offline tests cover execution ordering, collection windows, cancellation, late
results, transcript-prefix stability, review cleanup, and policy inheritance.
`scripts/verify_native_codex_async.py` exercises a compiled native binary against
a scripted local provider without account credentials or paid requests.

These checks establish behavior, not an efficiency percentage. Compare otherwise
identical tasks with Async tools off/on to measure model requests, usage, elapsed
time and solution quality. Keep the independent Completion change constant in
both arms.

The event-driven scheduling approach is inspired by
[Unreal Agent](https://github.com/unreallabsai/unreal-agent).
