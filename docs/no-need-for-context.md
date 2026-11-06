# No Need For Context

No Need For Context is Bello's optional context compaction for long runs. It is
off by default. When enabled it applies to the coder, revision coder, completion
reviewer, adversary and their subagents on Pi, Claude subscription (Agent SDK)
and native Codex. The runtime supervisor is never compacted. Enable it before a
fresh run; an existing thread keeps the policy it started with.

In `bello config`, enable `no-need-for-context`, then set the token threshold
and recent-tail target. New configurations use an absolute token target:

```json
{
  "no_need_for_context": {
    "enabled": false,
    "threshold_tokens": 244800,
    "retain_tokens": 24800
  }
}
```

Run flags:

```sh
bello --no-need-for-context --context-threshold-tokens 244800 --context-retain-tokens 24800
bello --no-no-need-for-context
```

The threshold and absolute retention target must be positive integers. The
absolute target is **24,800**, not 10% of the threshold (24,480). Existing explicitly
saved thresholds and `retain_percent` settings remain unchanged. Legacy percentage
mode still accepts a finite number greater than zero and at most 50, using
`retain_percent` or `--context-retain-percent`. Select only one retention mode;
the config editor lets you switch between them. Defaults apply only to omitted
settings, and changing the threshold does not change an absolute tail target.

While NNC is on, `retain_tokens` must be at most half of `threshold_tokens`, the same
bound as the 50% cap of `retain_percent`. With the default 24,800-token tail, a
threshold below 49,600 needs an explicit smaller `retain_tokens`
(`--context-retain-tokens`) or a `retain_percent`. Bello rejects other combinations
before a run starts, and the config editor reports them inline. Saved 0.8.0 settings
always contain `retain_percent` and are unaffected.

## What it does

When a role's input context reaches the threshold, Bello replaces the older
middle of that role's history with a summary written by the same model at the
same reasoning effort, without tools. The original task, instructions and tool
definitions stay in place. The most recent whole turns stay verbatim, targeting
`retain_tokens` (24,800 by default), or
`threshold_tokens × retain_percent / 100` in legacy percentage mode.
This tail target is soft: turns and tool calls with their results are never
split, so the tail can be larger. The latest human correction also stays
verbatim. Each role has its own history, threshold accounting and summary.

The summary is plain text; no JSON, envelope or headings are required. The model
is asked for at most about 16,000 characters, and a summary longer than 48,000
characters is rejected rather than truncated. The summary step has one
500-second deadline that includes its single retry. Empty, refusal-only,
truncated, tool-using, over-long and non-reducing summaries are rejected, and
the original history is kept unchanged. Backend checkpoint accounting prevents
the old summary input from immediately triggering the same compaction again;
native Codex's measured-usage behavior is described below. The summary is a
generative handoff, not a lossless copy of what it replaces.

The configured threshold is used as given and is never lowered automatically.
The configured threshold is validated against the selected model's reported
capacity, reserving its output allowance plus a safety margin. A threshold the
model cannot honor fails explicitly rather than being lowered. Backend-specific
request admission and protected-context behavior are described below.
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
  `native-codex-0.161.0-nnc-tokens-20261105` helpers include it and have passed platform
  qualification and local-archive installed-cache proofs on Linux x64, Windows
  x64 and macOS arm64. A helper without the capability still stops before the first
  model turn; Bello does not switch engines. The helper uses a
  4,000-token output reserve and a 4,096-token margin (the default threshold needs
  at least 252,896 usable tokens). The absolute tail also requires the
  `bello_context_retain_tokens` capability, which the pinned helpers advertise. Their
  context proof qualifies both the absolute tail (seven cases) and the legacy
  percentage tail (one case). See the [qualification and delivery details](native-codex-candidate.md)
  for the separate production-HTTPS and final installed-wheel checks; publication
  alone is not proof that those checks or live-model tests passed.

### Native Codex: measured trigger, no estimate-based refusal

The pinned helpers remove local refusals based on an approximate history-size estimate before the
summary request and before applying its replacement (including late results).
Opaque reasoning can make that estimate differ from the actual provider input;
it is not treated as an authoritative context-capacity measurement. The selected
summary request is sent unchanged. Real provider errors, including a real context
overflow, remain errors and keep the original history unchanged. This change
does not establish that every previously refused request would fit.

The trigger now uses the latest ordinary response's measured provider
`input_tokens + output_tokens`, rather than cumulative billing or estimates of
historical ciphertext. After a successful compaction its old measurement is reset
atomically with the durable checkpoint; the next ordinary provider response
measures the rebuilt context. Cold resume honors the same boundary without
resetting cumulative usage or losing the summarizer's billing records. Like
Unreal's usage checkpoints, newly staged local messages and tool results are not
counted by this trigger until the next provider response. This is simpler
accounting, not a guarantee against a genuine oversized request.

Approximate item sizes still select the soft tail and reject a non-reducing
summary. If no completed model round can safely be retired, compaction waits for
another boundary without splitting the protected round or failing on its
estimated size. A 24,800-token tail target is not an exact total-context promise:
the original prefix, summary and whole protected tool groups are additional.

Summary validity, cancellation and timeout handling are unchanged. Concurrent
history rewrites are still rejected; late items are retained; the complete
checkpoint must be persisted before replacement, and uncertain persistence still
blocks inference. Sandbox and permission behavior are unchanged. These changes
are native-Codex-only: Pi and Claude subscription accounting and admission behavior
above is unchanged; they share only the new defaults and absolute tail option.
