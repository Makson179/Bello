# Advice output contract

Return exactly one selected Bello setup as a concise, human-readable recommendation. The user should be able to understand the run without reading a `ProjectConfig` schema.

Use this structure:

```text
I recommend this Bello setup for `TASK.md`:

- Connection: the OpenAI subscription for all models below.
- Planning: no separate plan.
- Coding: GPT-5.6 Sol at high effort.
- Execution mode: normal speed.
- Runtime supervision: GPT-5.6 Sol at medium effort, with cheap runtime triage enabled.
- Completion review: GPT-5.6 Sol at high effort may return work to the coder once.
- Adversarial testing: off.
- Revision coder: off.
- Sub-agents:
  - Coder: up to 2; default Terra high; allowed Terra medium/high/xhigh and Sol medium/high.
  - Completion reviewer: up to 2; default Terra high; allowed Terra medium/high/xhigh and Sol medium/high.
- Workspace: keep existing state, do not clean the workspace, and use no protected paths.

If you approve, I can apply this configuration and run Bello.
```

This is a shape example, not a preset. Populate it from the selected setup and omit irrelevant detail:

- Write the recommendation in the user's language.
- State the exact normalized project-root-relative task path.
- For planning, say either that no separate plan is needed or name the exact planner model, effort, and `PLAN.md` output. Planning is prepared before Bello and passed with `--plan`.
- State the coder, runtime supervisor, and every active reviewer as model plus effort and its provider/billing route in compact readable terms. For example, distinguish Claude through the Claude Code subscription from Claude through OpenRouter. Do not bury that distinction in raw config keys. Mention Fast and cheap runtime triage in readable terms.
- Describe the review schedule as maximum allowed returns and adversary passes. For an adversarial schedule, state separately how many completion returns are allowed before the first adversary, how many adversary passes may run, and how many completion returns are allowed after each adversary pass. Make clear that a reviewer may accept earlier when that distinction matters.
- Whenever adversarial testing is active, name the completion model and effort that processes adversary reports even when no ordinary completion-return stage is scheduled.
- State whether revision coder is off; when it is on, give its model and effort. Never show its dormant profile when it is off.
- Describe sub-agents separately for each enabled parent role. Give maximum concurrency, the default child profile, and the allowed profile pool in compact prose. Do not show the policy object for a disabled role.
- If all sub-agent policies are disabled, say simply that sub-agents are off.
- Summarize `start_over`, `clean`, and `protected_path` as workspace behavior rather than raw fields.

Do not emit JSON, raw config keys, compatibility markers such as `review_limit_format`, dormant role profiles, disabled policy objects, zero-valued implementation fields, runtime-owned state, or a machine-readable advice wrapper. Do not include task analysis, internal reasoning, alternatives, cost or time estimates, quality predictions, model comparisons, sources, citations, confidence statements, or validation narration.

Build one complete `ProjectConfig` internally, including every field required by [CONFIG_SCHEMA.md](CONFIG_SCHEMA.md), and validate it silently with `scripts/validate_config.py --file PRIVATE_CONFIG --catalog CATALOG`. When commands are allowed, also pass `--project-root PROJECT_ROOT --task-file TASK_FILE` so the validator checks that the internal config names the exact resolved task. If validation or a checked installed-version compatibility test fails, return only the concise blocker instead of a recommendation. When the user later approves the setup, reconstruct the same active setup from the recommendation, canonicalize hidden dormant fields according to [CONFIG_SCHEMA.md](CONFIG_SCHEMA.md), validate the complete config again, and do not silently change active profiles or the review schedule.
