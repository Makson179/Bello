# No Need For Context

No Need For Context is Bello's optional context compaction for long runs. It is
off by default. When enabled it applies to the coder, revision coder, completion
reviewer, adversary and their subagents on Pi, Claude subscription (Agent SDK)
and native Codex. The runtime supervisor is never compacted. Enable it before a
fresh run; an existing thread keeps the policy it started with.

In `bello config`, enable `no-need-for-context`, then set the token threshold
and recent-tail percentage. The saved configuration is:

```json
{
  "no_need_for_context": {
    "enabled": false,
    "threshold_tokens": 249000,
    "retain_percent": 10
  }
}
```

Run flags:

```sh
bello --no-need-for-context --context-threshold-tokens 249000 --context-retain-percent 10
bello --no-no-need-for-context
```

The threshold must be a positive integer. The retention percentage must be a
finite number greater than zero and at most 50.

## What it does

When a role's input context reaches the threshold, Bello replaces the older
middle of that role's history with a summary written by the same model at the
same reasoning effort, without tools. The original task, instructions and tool
definitions stay in place. The most recent whole turns stay verbatim, targeting
`threshold_tokens × retain_percent / 100` tokens (24,900 with the defaults).
This tail target is soft: turns and tool calls with their results are never
split, so the tail can be larger. The latest human correction also stays
verbatim. Each role has its own history, threshold accounting and summary.

The summary is plain text; no JSON, envelope or headings are required. The model
is asked for at most about 16,000 characters, and a summary longer than 48,000
characters is rejected rather than truncated. The summary step has one
500-second deadline that includes its single retry. Empty, refusal-only,
truncated, tool-using, over-long and non-reducing summaries are rejected, and
the original history is kept unchanged. After a checkpoint or a failed attempt,
the next attempt waits for the context to grow; this schedule is saved with the
thread, so a resumed thread does not repeat a compaction. The summary is a
generative handoff, not a lossless copy of what it replaces.

The configured threshold is used as given and is never lowered automatically.
The input budget reserves the model's output allowance plus a safety margin. A
threshold the selected model cannot honor, or protected context and recent
tool groups that cannot fit, fail explicitly with the original context kept.
Summary requests count toward the turn's usage exactly once, including a
cancelled summary; when a cancelled summary's usage cannot be read, the turn is
marked usage-incomplete. Cancellation never leaves a turn without a final state.

Compaction changes conversation history only. Sandbox, readable roots, network,
approvals and escalation behave exactly as with the feature off. Replacing the
middle changes the provider prompt: an unchanged prefix can still be cached
where the provider supports it, and later sections are rebuilt.

## Backends

- **Pi** compacts at a turn boundary inside the worker and records each
  checkpoint in the session journal; an interrupted checkpoint is rolled back
  when the session is reopened. The input budget is the model's context window
  minus its output allowance and a 4,096-token margin.
- **Claude subscription** continues in a new Agent SDK session that holds the
  retained turns and the summary, on the same subscription route. Earlier
  assistant and tool activity becomes historical context; native session
  checkpoints and thinking signatures are not carried over. A summary may use up
  to 64,000 output tokens, and the request is admitted only if it fits the
  reported context capacity with that allowance and a safety margin.
- **Native Codex** requires a helper that advertises the
  `bello_context_compaction` capability. The pinned
  `native-codex-0.161.0-nnc-se-20261019` helpers include it and have passed platform
  qualification and local-archive installed-cache proofs on Linux x64, Windows
  x64 and macOS arm64. A helper without the capability still stops before the first
  model turn; Bello does not switch engines. The helper uses a
  4,000-token output reserve and a 4,096-token margin (the default threshold needs
  at least 257,096 usable tokens). See the [qualification and delivery details](native-codex-candidate.md)
  for the separate production-HTTPS and final installed-wheel checks; publication
  alone is not proof that those checks or live-model tests passed.
