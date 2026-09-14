# Current Bello configuration schema

This reference targets Bello 0.6.0's user-facing `ProjectConfig`. The persistent file is `.supervisor/config.json`, but it also contains runtime state after a run starts. Never replace that file directly or feed it directly to the advisor validator; first extract the project settings with `scripts/inspect_config.py`.

The inspector deliberately preserves valid dormant values such as saved review budgets, cheap-runtime preference, and a distiller path while their feature is off. Its `current_project_config` is for safe application after selection, not evidence for choosing the setup, and is not necessarily valid under the advisor's stricter canonical output policy. Overlay the selected switches and review fields, zero inactive budgets, and set cheap runtime false when runtime is off before running `validate_config.py`. The inspector checks source syntax, not model capabilities or bundle availability.

## Fully resolved shape

```json
{
  "review_limit_format": "explicit",
  "task": "TASK.md",
  "coder_mod": "openai-codex/gpt-5.6-luna",
  "revision_coder_enabled": false,
  "revision_coder_mod": "openai-codex/gpt-5.6-luna",
  "runtime_mod": "openai-codex/gpt-5.6-luna",
  "completion_mod": "openai-codex/gpt-5.6-luna",
  "adversary_mod": "openai-codex/gpt-5.6-luna",
  "coder_intelligence": "xhigh",
  "revision_coder_intelligence": "xhigh",
  "runtime_intelligence": "high",
  "completion_intelligence": "xhigh",
  "adversary_intelligence": "xhigh",
  "speed": "usual",
  "runtime_enabled": true,
  "cheap_runtime": true,
  "log_distiller": {
    "enabled": false,
    "model_path": null
  },
  "start_over": false,
  "completion_review": false,
  "adversary": false,
  "max_adversary_runs": 0,
  "max_completion_returns_before_adversary": 0,
  "max_completion_returns_after_adversary": 0,
  "clean": false,
  "protected_path": [],
  "multi_agent": {
    "enabled": false,
    "max_concurrent": 4,
    "default": {
      "model": "openai-codex/gpt-5.6-luna",
      "intelligence": "high"
    },
    "allowed": {
      "openai-codex/gpt-5.6-luna": ["medium", "high", "xhigh"],
      "openai-codex/gpt-5.6-terra": ["medium", "high"]
    }
  },
  "completion_multi_agent": {
    "enabled": false,
    "max_concurrent": 4,
    "default": {
      "model": "openai-codex/gpt-5.6-luna",
      "intelligence": "high"
    },
    "allowed": {
      "openai-codex/gpt-5.6-luna": ["medium", "high", "xhigh"],
      "openai-codex/gpt-5.6-terra": ["medium", "high"]
    }
  },
  "adversary_multi_agent": {
    "enabled": false,
    "max_concurrent": 4,
    "default": {
      "model": "openai-codex/gpt-5.6-luna",
      "intelligence": "high"
    },
    "allowed": {
      "openai-codex/gpt-5.6-luna": ["medium", "high", "xhigh"],
      "openai-codex/gpt-5.6-terra": ["medium", "high"]
    }
  }
}
```

Always include `"review_limit_format": "explicit"`. Without it, older zero-budget semantics are migrated to `"unlimited"`.

Complete recommendations require every shown root field and both `log_distiller` fields. Sparse saved configs remain supported: missing `runtime_enabled` defaults to true; missing `log_distiller` or its fields defaults to `{"enabled": false, "model_path": null}`. Completion and adversary retain their own switches; neither is inferred from runtime or distiller.

## Providers, models, and effort

The JSON shape above is an illustration, not a preset. Choose models from the current `scripts/inspect_models.py` output. Pass the same catalog to `scripts/validate_config.py --catalog CATALOG` to check active role and child profiles.

Use exact qualified identifiers:

| Route | Identifier form | Billing |
| --- | --- | --- |
| OpenAI subscription | `openai-codex/<model>` | Subscription usage |
| Claude Code subscription | `claude-code/<model>` | Subscription usage |
| OpenAI API | `openai/<model>` | API |
| Anthropic API | `anthropic/<model>` | API |
| OpenRouter | `openrouter/<author>/<model>` | OpenRouter API |
| Other supported Pi provider | `<provider>/<model>` copied from the catalog | Provider API |

The first slash separates the provider; the remaining model ID may contain more slashes. Preserve case and the exact catalog identifier. Legacy bare `gpt-...` IDs map to `openai-codex` for compatibility, but new advice uses qualified IDs. Do not infer a billing route from the model's author.

Models, supported efforts, and service tiers are dynamic. Never use a fixed three-family whitelist or assign every provider the same effort values. Validate active settings against the supplied catalog; `off`, `minimal`, or `ultra` are legitimate only when that exact profile exposes them. Catalogue compatibility does not establish relative capability or remaining quota.

Dormant fields are still structurally required. Canonicalize disabled role profiles to a valid active profile, and disabled child policies to a valid one-profile pool, without displaying them. Their providers do not need separate authentication while unused. Every enabled child pool must contain its default profile and use available, supported models and efforts. Parents and children need not share a provider.

For price, speed, and quality evidence, use [MODEL_ECONOMICS.md](MODEL_ECONOMICS.md); the runtime catalog is not a performance ranking.

## Planning stage

Planning is an optional pre-run decision, not a `ProjectConfig` field. Never add `plan` or `plan_path` to the fully resolved JSON.

When a separate plan is recommended, use the host's supported planning workflow with the selected planner profile to create one advisory Markdown file and pass it at launch with `bello --task TASK.md --plan PLAN.md`. Bello does not create the plan. The initial coder must treat it as a working hypothesis, verify it against the repository, and deviate when implementation reveals facts or nuances the planner could not know. Do not create a replanning loop.

The plan must be a regular `.md` file inside the project root, distinct from the task, not named `AGENTS.md`, not a link or hardlink, and outside Bello runtime, cache, and dependency directories. In a Git repository it must be untracked and absent from reachable Git history. Bello stages a guarded copy for the initial coder, excludes it from the resulting patch, removes it before switching to a revision coder, and keeps completion review, adversary, and adversary-report normalization plan-blind.

## Runtime and log-distiller switches

`runtime_enabled`, `completion_review`, `adversary`, and `log_distiller.enabled` are four independent Booleans. Any combination is supported, subject to each feature's own model, budget, or backend requirements.

Runtime supervision defaults on. With `runtime_enabled: false`, use `cheap_runtime: false`: no runtime agent, cheap-triage calls, runtime self-tests, semantic approval decisions, steering, or behavioral-readiness gate runs. User messages go directly to the coder; separately enabled completion and adversary checks still run. This is a lower-protection mode, not equivalent live supervision for less cost. Filesystem sandboxing, protected paths, protocol/lifecycle checks, snapshot integrity, child lifecycle handling, and final-patch handling remain. Network access is preauthorized inside the filesystem sandbox; unsupported outside-sandbox requests are denied without a hidden approval model or automatic `danger-full-access` grant.

Log distiller defaults off. It selects original excerpts from eligible coder and coder-child tool output on local CPU; it is not a generative summary or another provider/model role. Unchanged logs remain available to the controller as evidence. Enable it for a task-specific reason, not a guaranteed saving. Each request has a 300-second total deadline including queue time; failures, timeouts, or unusable selections retain the ordinary bounded result.

`log_distiller` contains exactly `enabled` (Boolean) and `model_path` (non-empty local folder path or null). With `enabled: true` and `model_path: null`, an approved run downloads Bello's pinned default model from Hugging Face (about 599 MB) once and reuses the cache, including offline. An explicit path selects a local compatible bundle instead; relative paths resolve from the project root. A disabled saved path may remain dormant. The advisor validator accepts the default without downloading it; for an explicit path it checks manifest compatibility and required local files without loading/hashing weights or making provider calls. This does not prove runtime readiness: `log-distiller` dependencies must be installed, and subscription Codex requires the compatible native selection-hook executable. Report unmet setup requirements without installing or downloading anything during advice. The worker verifies checkpoint hashes and tensors at use.

Changed runtime/distiller settings require a fresh run when persisted threads have a different tool contract. Do not silently resume with altered policy, clear history, or restart without authorization. An ordinary transport restart or revision-coder switch within a run retains the run's selected settings.

## Review scheduler semantics

Completion and adversary have independent switches and three budgets; runtime may be on or off for any final-review schedule. The following modes are frequent baselines, not a closed menu:

| Mode | `completion_review` | `adversary` | Returns before A | A passes | Returns after A |
| --- | --- | --- | ---: | ---: | ---: |
| No final review | `false` | `false` | 0 | 0 | 0 |
| `C` | `true` | `false` | 1 | 0 | 0 |
| `A` | `false` | `true` | 0 | 1 | 0 |
| `C+A` | `true` | `true` | 1 | 1 | 0 |

For `A`, completion is genuinely off; there is no hidden completion-model requirement. No final review with runtime on is often called `runtime-only`; with runtime off, the coder has no semantic supervisor or final reviewer. These modes describe bounded opportunities, not guaranteed literal model-call sequences.

The underlying values remain a general scheduler. Select a custom schedule whenever concrete task evidence or an explicit user preference justifies it; there is no need to prove that every baseline fails first. Every additional return or adversary pass should have a distinct reason and an acknowledged time cost.

- With `completion_review: false`, set both completion-return budgets to `0`. Keep the independently selected adversary switch and pass budget.
- With `completion_review: true` and `adversary: false`, `max_completion_returns_before_adversary` is the maximum number of completion-review **return decisions** before Bello completes after the coder's next readiness. Set adversary runs and post-adversary returns to `0`.
- With both switches true, `max_completion_returns_before_adversary` bounds completion-review returns before the first adversary. An earlier completion accept starts the adversary immediately; reaching the return limit also starts it without another completion-review call.
- `max_adversary_runs` is the maximum number of adversary passes. It must be positive when adversary is active.
- `max_completion_returns_after_adversary` bounds completion-review returns after each adversary pass. At the limit Bello starts another remaining adversary pass or completes after the final pass. An earlier completion accept advances immediately.
- Every adversary report is separately processed by `adv_report_controller`, using the completion profile when completion is enabled, otherwise the adversary profile. Its normalization call is not itself one of the completion-return budget units, but it adds cost and time and can return a genuine finding to the coder. A-only completes after a no-finding report or exhaustion of its pass budget following coder repair; it does not claim completion-review acceptance.

Each return budget accepts any supported non-negative integer or `"unlimited"`; adversary runs accept any non-negative integer. Therefore schedules commonly abbreviated as `2C+A`, `C+A+C`, repeated adversary cycles, and many other combinations are expressible. These abbreviations describe configured opportunities, not a guaranteed literal call sequence: reviewers may accept early, an adversary may find nothing, or a finding may send the coder back into the loop.

A zero pre-adversary budget with a positive adversary budget skips the preceding completion-review call. Always keep `"review_limit_format": "explicit"`; otherwise legacy loading can reinterpret zero limits as unlimited.

## Revision-coder semantics

`revision_coder_enabled`, `revision_coder_mod`, and `revision_coder_intelligence` configure an optional one-time profile switch for review-driven repair:

- With `revision_coder_enabled: false`, completion-review and normalized adversary findings are delivered to the current coder thread.
- With `revision_coder_enabled: true`, the first completion-review return or the first finding returned by `adv_report_controller` quiesces the current coder tree and starts one fresh coder thread with the configured revision model and effort. This planned switch is not a health restart and does not consume the health-restart budget. Later reviewer findings and runtime steering reuse that revision thread; Bello does not create a new revision thread for each stage.
- The revision coder works in the same candidate workspace and receives the persisted handoff plus the triggering reviewer feedback. It uses the coder's `multi_agent` policy; there is no separate revision-coder multi-agent object.
- The revision profile is dormant when no reviewer finding is returned, including any schedule with neither completion nor adversary. It may be enabled for A-only regardless of runtime. Any available supported provider/model profile remains eligible, including a different provider from the initial coder.

The model and effort fields remain structurally required even while the switch is disabled. When omitted from a sparse saved config, Bello defaults the revision model to the selected coder model and its effort to the selected coder effort.

## Multi-agent invariants

Bello has three independent multi-agent policy objects with the same shape:

- `multi_agent` controls the initial coder and, after a profile switch, the revision coder;
- `completion_multi_agent` controls only completion-review threads;
- `adversary_multi_agent` controls only adversary threads.

For every object, `max_concurrent` is a positive integer. The default child model/effort pair must appear in that object's `allowed` map, and every allowed effort must be valid for its model. Enabling or editing one policy does not change either of the other two.

The advisor owns each role's `enabled` recommendation: it must choose `true` or `false` from the task evidence and user constraints rather than passively preserving the baseline switch. A `false` recommendation still carries a structurally valid child policy, but those child-policy values are dormant.

Coder children may perform bounded implementation or investigation work. Completion-review children may inspect independent requirements, modules, or validation questions, while the parent completion reviewer retains the final accept-or-return judgment. Adversary children may probe independent attack surfaces or failure hypotheses, while the parent adversary retains the final report judgment. Reviewer delegation is one child level deep, and the parent independently verifies relevant child findings.

Runtime supervision and `adv_report_controller` do not have multi-agent policy objects. In particular, `completion_multi_agent` is not used by adversary-report normalization. It is dormant in an adversary-only (`A`) schedule because that schedule has no completion-review call; `adversary_multi_agent` may still be active for its adversary pass.

This illustrates a child policy shape, not a required default or provider choice:

```json
{
  "enabled": true,
  "max_concurrent": 2,
  "default": {
    "model": "openai-codex/gpt-5.6-luna",
    "intelligence": "high"
  },
  "allowed": {
    "openai-codex/gpt-5.6-luna": ["medium", "high", "xhigh"],
    "openai-codex/gpt-5.6-terra": ["medium", "high"]
  }
}
```

For any enabled policy, set `max_concurrent` to the number of named independent workstreams, normally no more than four. Do not add expensive child profiles without a task-specific reason. Child and parent profiles are chosen independently; an inexpensive model may be used in either when it is sufficient.

## Task path

The advisor always owns `task` in its returned recommendation. Resolve the current task independently of any saved Bello configuration, require it to be a regular file inside the project root, and emit its normalized project-root-relative path with `/` separators. `task` must never be `null`, empty, absolute, drive-relative, UNC, contain `..`, or retain a temporary-workspace prefix. A nested path such as `tasks/TASK.md` is valid.

The inspector may return `task: null` as a baseline when the saved configuration has no task. Before validating or returning advice, replace that baseline with the current resolved task path. When command execution is allowed, pass `--catalog`, `--project-root`, and `--task-file` to `scripts/validate_config.py` so it can verify that the JSON names the exact input file.

## Safe project-field defaults

- `start_over`: emit `false` for advice. It controls Bello history/recovery state and may be changed only when the user explicitly requests it during apply.
- `clean`: emit `false`; use `true` only after the user confirms that this run's workspace is disposable.
- `protected_path`: derive only from an explicit task requirement or user preference; otherwise emit `[]`. Bello denies both reads and writes to these roots, so this is not a read-only-file mechanism. Do not add the task file, whose integrity Bello already checks, or a reference executable that the task requires the coder to use.
- `speed`: use `usual`; `fast` requests the `priority` service tier, not a universal speed boost. It affects coder, active revision coder, runtime, completion/report-controller, and applicable child turns. Use it only when all affected profiles expose that tier. In particular, do not assume the official Claude Code backend accepts Bello's Fast setting.

`cheap_runtime` is a Boolean, canonicalized to false when runtime is off. The default triage route is `openai-codex/gpt-5.6-luna`; `BELLO_RUNTIME_TRIAGE_MODEL` is an advanced existing model override. There is no separate triage model/effort field in `ProjectConfig`, and triage requests do not specify an effort. Enable triage only when runtime is on and its effective route is available and allowed by the user's preferences. For a Claude-only setup without that route, choose `false`; do not invent an extra subscription or a saved field. Do not assign triage `high` or any other fixed effort. Do not change environment overrides just to make a recommendation work.

The validator uses that default triage route. If an existing `BELLO_RUNTIME_TRIAGE_MODEL` override has been explicitly verified, pass its exact qualified ID with `--triage-model`; that option validates the existing route and does not configure or change it.

## Current application limitation

The current run CLI exposes initial-coder, runtime, completion, and adversary models and efforts; `--runtime/--no-runtime`; review/adversary toggles; `--log-distiller/--no-log-distiller` and `--distiller-model PATH`; adversary count; speed; task; an optional private `--plan PATH`; start-over; clean; and protected paths. These one-run flags override the corresponding saved values without rewriting the project config. `--plan` has no persistent config counterpart and applies only to that run.

It does not expose revision-coder enablement/model/effort, cheap runtime, completion-return budgets, or any of the three multi-agent policy objects as one-run flags. Those fields require the saved `bello config` interface. A one-run initial-coder override does not implicitly rewrite the persisted revision-coder profile.

The 0.6.0 `bello-delegate` helper starts the installed Bello with the already selected saved configuration and explicitly approved task/plan paths. It does not select models or apply configuration itself. Finish the authorized configuration step before delegating the launch; do not silently drop settings to fit an older plugin.
