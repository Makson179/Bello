# Selection policy

Derive the objective from the user's request. Cost, expected quality, and time are separate axes. A request such as “cheaper,” “as strong as practical,” or “finish sooner” is part of the input and should visibly change the recommendation. When the user gives no ordering, seek low whole-trajectory cost while retaining strong expected quality.

## Decision evidence

Use concrete facts and uncertainties that can change a configuration choice:

- the central implementation and whether a nearby command, adapter, data flow, subsystem, or other existing analogue constrains the intended architecture;
- task requirements that are ambiguous, coupled, or independently missable;
- task-relevant edge cases, invalid inputs, interactions, protocols, concurrency, security, recovery, or compatibility behavior;
- consequences of mistakes and any safety or reversibility constraints;
- useful delegation work such as implementation branches, repository investigation, requirement audits, or independent attack surfaces.

Do not assign `low`, `medium`, or `high` difficulty labels to the task or to these dimensions. Repository size, task length, or estimated changed lines alone do not determine a model or schedule. Keep missing information as an explicit unknown instead of automatically buying a stronger pipeline.

Ignore the project's tests, test directories, CI configuration and results, Bello benchmark artifacts, previous run outcomes, telemetry, and saved Bello configuration when selecting the setup. Public model comparisons in official documentation are permitted model information. Do not make their presence or absence change the recommendation. A testing requirement written in the task remains task evidence, but the existing test suite is not an additional input.

## Planning decision

Make one explicit planning decision before choosing the execution profile:

- Recommend a separate plan when task-wide architecture, coupled requirements, ordering, or cross-subsystem constraints can be resolved meaningfully before implementation, and one dedicated planning pass is likely to reduce enough execution work or risk to repay its own cost and time.
- Skip a separate plan for a small local change, when a nearby implementation already supplies the needed structure, or when the decisive facts can only be discovered by editing and running the code.
- When planning is recommended, the planner's expected task-relevant capability must be at least as high as the initial coder's. Never choose a weaker planner to save cost or time. Within one family, the planner must use the coder's effort or a higher one. Across families, use a different planner only when current evidence makes its non-inferiority defensible; if the ordering is uncertain, use the exact coder profile or a clearly stronger same-family profile for planning. The expected reduction in execution cost or risk must still repay the extra call, and family names alone do not prove capability dominance.
- Use the host's supported planning workflow with the selected planner profile, only after approval. Its only artifact is one advisory Markdown plan; do not create a custom planner service, planning agent chain, or replanning loop. If that profile cannot be selected in the host, report the limitation before execution rather than use the current frontend model silently.
- The executor must verify the plan against the task and repository and may deviate when implementation reveals facts or nuances the planner could not know. A plan can reduce architecture-discovery burden, but it is not a reason to choose an executor incapable of checking and adapting it.

Planning is separate from Bello's persistent project configuration. When a recommendation is later applied, pass the private plan as a run-only `--plan` input. Keep it untracked and absent from reachable Git history so Bello can keep completion review, adversary, and a revision coder plan-blind.

## Roles

Choose each role for the judgment it must perform rather than copying a preset profile.

- **Initial coder:** forms the architecture and first candidate. A strong review pipeline cannot rescue a coder that is unlikely to discover the central solution.
- **Revision coder:** when enabled, takes over after a reviewer or adversary returns the candidate and continues handling later correction cycles in its revision thread. It may be cheaper than the initial coder when the remaining work is mainly applying concrete findings. Keeping it disabled avoids the thread handoff and preserves the initial coder's context.
- **Runtime supervisor:** observes the live trajectory, commands, edits, validation, approvals, drift, and material risks, and steers when needed. Cheap runtime is a triage layer, not a replacement for this parent.
- **Completion reviewer:** independently compares a claimed-ready candidate and its validation evidence with the whole task. It may accept or return concrete missing, incorrect, or insufficiently validated work.
- **Adversary:** attacks an isolated candidate through task-relevant counterexamples and failure surfaces. Its report may return real findings to the coder, but adversarial testing is not itself a general completeness proof.

Every available provider/model profile is eligible for primary roles whose interface it supports. Choose each role's model and effort from the judgment it must perform, the user's objective, and the expected trajectory. Do not impose a provider or family floor merely because a role is a reviewer; an inexpensive completion reviewer or adversary can be the right choice in a cost-sensitive configuration. Reviewer parents remain responsible for synthesis, final judgment, and the report when they delegate bounded work to children.

### Review schedules

The following are frequent useful baselines, not the only valid choices:

- **`runtime-only`:** live supervision with no completion-review return or adversary pass;
- **`C`:** one bounded completion-review return opportunity;
- **`A`:** one adversary pass with no completion-review return before or after it;
- **`C+A`:** one completion-review return opportunity followed by one adversary pass.

Bello's underlying scheduler is general. Select `2C+A`, `C+A+C`, multiple adversary cycles, or another supported schedule whenever a concrete residual risk or user preference makes the extra opportunity worthwhile. There is no need to prove that every common baseline fails first. Internally tie each additional return or pass to a concrete purpose and account for its possible serial reviewer call, coder repair, and revalidation.

Review budgets are upper bounds, not guaranteed call counts. An early accept can advance the schedule, while a useful finding can return work to the coder. The adversary-report controller also adds a normalization call even though it is not a completion-return budget unit.

## Model and effort choices

Before comparing profiles, run `scripts/inspect_models.py`. It reads Bello's available multi-provider catalog, not the frontend host's model list. Choose exact qualified identifiers and supported effort values. Preserve the route: a subscription model and an API model with a similar name are not interchangeable.

Read [MODEL_ECONOMICS.md](MODEL_ECONOMICS.md) and refresh the relevant official documentation. Use published prices, model positioning, numerical comparisons where available, and reasonable approximate internal judgments where not. Exact comparative measurements are not a prerequisite for recommending a setup. Do not replace current information with a fixed Sol/Terra/Luna ranking or invented cross-provider multipliers.

Treat every choice as a **provider/model/effort profile**. Reasoning labels are provider- and model-specific; same-named levels across providers need not consume similar time or tokens. Use current exposed capabilities rather than a universal effort ladder. A model can be sufficient at a lower effort when a nearby production implementation supplies the structure. An optional planner still must not be weaker than the coder for the task; uncertain cross-model ordering favors the same profile or a clearly stronger one.

### Initial and revision coders

Choose the initial coder from the reasoning needed to form or execute the solution, domain novelty, coupling, existing analogues, the planning decision, and user preference. Treat inexpensive models as legitimate options rather than fallbacks by definition. A useful private plan or a clear nearby analogue can make a cheaper coder appropriate, provided that coder can still verify assumptions and respond to implementation evidence.

Choose the revision coder for applying findings that completion or adversary has already made concrete. It is dormant in runtime-only runs, so keep it disabled when no review stage can return work. With reviews active, enable it only when bounded findings are plausible and one or more likely correction cycles make a cheaper fresh thread worth the handoff; a single likely-small repair normally stays with the initial coder. Do not use it as planned capability escalation: a recommendation must not make the revision profile more capable than the initial coder. If the initial profile may be unable to form the architecture, strengthen the initial profile instead of expecting review to rescue it later. Prefer a cheaper revision profile when the expected repairs are bounded and explicit; use the same profile or leave revision disabled when findings may require architectural reasoning, broad redesign, or the original thread's context.

Reviewer parents may use a more reasoning-intensive profile than either coder because discovering omissions and adversarial failures is their independent job, but they may also use an inexpensive model when the bounded review judgment and cost objective make it sufficient. A demanding review profile does not imply the repair role needs equal capability once the reviewer has supplied a precise diagnosis. Avoid obvious upward revision steps such as a higher effort on the same model. For cross-family profiles whose capability ordering is unclear, resolve the uncertainty in favor of the initial coder rather than building a weak-first/strong-later repair strategy.

### Reviewers

Choose completion effort for independent requirement and evidence adjudication. Choose adversary effort for generating and evaluating plausible attacks. Choose runtime effort for the live steering and risk decisions expected during implementation. Do not mirror the coder's model or effort automatically.

Add completion or adversary because it addresses a named gap, not as a generic quality ritual. A completion review is useful for independently missable requirements and weak final evidence; an adversary is useful when plausible candidates can be broken by interactions, invalid inputs, boundary conditions, protocols, concurrency, security, or similar attack surfaces.

## Multi-agent decisions

Make three separate decisions:

1. **Coder multi-agent:** useful for independent implementation pieces, repository investigation, or a requirement audit that helps the active coder. It need not imply several children editing separate files.
2. **Completion multi-agent:** useful when independent requirement groups, platform behavior, or subsystems can be checked separately. Children gather bounded evidence; the completion parent decides accept versus return.
3. **Adversary multi-agent:** useful when distinct attack surfaces or probe families can be explored independently. Children propose and execute bounded attacks; the adversary parent judges and synthesizes findings.

Do not enable a role merely because sub-agents are available. Enable it when the expected evidence or parallelism repays delegation, context, and integration overhead. For each enabled role:

- set `max_concurrent` to the number of useful simultaneous delegated jobs, subject to the user's resource and time constraints;
- choose the cheapest sufficient default child model and effort;
- expose a small allowed model-effort map that lets the parent select a more reasoning-intensive child only when a delegated job warrants it;
- enable the policy only when the intended child work is concrete enough to justify the numeric setting.

The coder, completion, and adversary policies are independent. Disabling one must not silently disable or clone another. When a role's multi-agent policy is disabled, its saved child fields are dormant and need no detailed defense in the human explanation.

## Select one configuration

Recommend exactly one concrete configuration shaped by the user's objective and the inspected task evidence. Do not make the user choose among a cheap, balanced, and strong menu. Resolve the tradeoff yourself, and ask one focused question only when a missing constraint would materially change the answer.

Reject a candidate when it is unlikely to form the central solution, leaves a named validation or attack risk untreated, violates a hard constraint, or adds a costly stage without a task-specific reason.

Compare the whole trajectory, not only model prices. Include an optional planning pass, runtime escalation, completion calls, adversary calls, adversary-report normalization, reviewer returns, revision-thread switching, repeated validation, and sub-agent work. Review stages are mostly serial around the coder and can dominate elapsed time even if their models are cheap. Use `speed: usual` unless the user prioritizes time, accepts the associated cost, and every affected active profile and allowed child supports Bello's requested Fast service tier.

Use only the four allowed inputs: the resolved task, production repository, current model/effort information, and user preferences. Do not consult or transfer conclusions from project tests, CI, Bello benchmarks, previous runs, telemetry, or saved configuration. Compare the complete proposed trajectory using current model documentation and approximate role-fit judgments. Treat any quality ordering as an internal estimate rather than a guarantee.

## Guardrails

- Bello does not enforce a hard dollar or token cap; describe a numeric budget as a planning constraint.
- Do not promise an optimal, guaranteed, or budget-capped result.
- Do not choose `clean: true`, destructive settings, or unbounded review budgets as generic quality boosts.
- Treat `protected_path` as a no-access grading/hidden root, not as a read-only path. A task-provided reference executable that the coder must query must remain accessible.
- Preserve fields outside the advisor's scope and require explicit authorization before mutating or launching anything.
