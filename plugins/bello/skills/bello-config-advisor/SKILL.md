---
name: bello-config-advisor
description: Inspect a coding task, repository, and user preferences, then recommend one concrete Bello configuration and decide whether a separate planning pass should precede execution. Use when the user asks to choose or optimize a Bello setup before a run, from Codex, Claude Code, or another host. Do not use merely to monitor a run whose configuration is already fixed.
---

# Bello Config Advisor

Use the task file, production repository, current model/effort information, and the user's quality, cost, and time preferences to choose one usable Bello setup. Include an explicit planning decision. With no stated preference, favor the least expensive setup that still has strong expected quality.

This skill targets Bello 0.6.0. The host where the user talks to you does not determine Bello's models or billing routes. Advice is read-only: do not apply settings, authenticate, install dependencies, create a plan, or start a run until the user asks.

## Inspect

1. Read [SELECTION_POLICY.md](references/SELECTION_POLICY.md). Use [CONFIG_SCHEMA.md](references/CONFIG_SCHEMA.md) to construct the internal config and [OUTPUT_CONTRACT.md](references/OUTPUT_CONTRACT.md) for the response.
2. Run `python <SKILL_DIR>/scripts/inspect_models.py`, using the Python executable available in the host. It reads `bello runtime models --engine all`, not the host agent's model list. Keep the result in a temporary file outside the project for `validate_config.py --catalog`. No model generation is requested. A supplied catalog can be read with `--file`. Do not silently replace an unavailable catalog with a fixed list of OpenAI models.
3. Read [MODEL_ECONOMICS.md](references/MODEL_ECONOMICS.md). Refresh the relevant official model pages for price, capability, speed, and effort. The OpenAI, Claude, and OpenRouter catalogs are entry points, not three pages to read exhaustively on every task. Read details for plausible available profiles, and use approximate comparisons internally where exact measurements are absent. Account for the user's available subscriptions/API connections and spending preferences; never read or print credentials.
4. Resolve the project root and task file. Read the task from its file. Require a regular file inside the project and normalize `task` to its project-root-relative path with `/` separators.
5. Inspect task-relevant production files: applicable workspace instructions, manifests, interfaces, existing implementations, and the current diff where useful. Nearby implementations are important: they may supply the architecture and make a cheaper executor sufficient. Use `rg --files` or `git ls-files` first. Ignore the project's tests, CI, benchmark artifacts, previous runs, telemetry, hidden Bello history, and saved configuration when selecting the setup. A testing requirement in the task remains part of the task. Public model comparisons in the official documentation are model information, not project-run evidence.
6. Extract hard constraints and softer preferences. Ask one focused question only when missing information materially changes the choice; otherwise make a reasonable internal assumption. Do not inspect the saved Bello config until the user asks to apply or launch the chosen setup.
7. Do not run tests, builds, installers, the task's code, configuration editors, or plugin updates just to make a recommendation.

## Choose

- Select one setup, not a menu. Compare quality, total cost, and time across the whole run, including planning, reviews, repairs, report normalization, and sub-agent coordination.
- Choose every active role independently from the available provider/model/effort profiles. Any supported model can be a coder, reviewer, adversary, or child when sufficient for that role. Do not impose a provider/family floor or assume a more expensive model is always better.
- Preserve provider identity: `openai-codex/...` and `claude-code/...` use their subscription routes; `openai/...`, `anthropic/...`, `openrouter/...`, and other API routes are distinct choices. Do not substitute an API route for a subscription or the reverse. Parents and children can use different providers when the corresponding connections are available.
- Treat model and effort as separate choices. Use the exact supported efforts in Bello's catalog and provider-specific explanations. A label such as `high`, `max`, or `ultra` does not imply the same behavior across providers, nor does it automatically enable Bello sub-agents.
- Recommend a separate planning pass only when its expected benefit repays its cost and time. The planner must not be less capable than the initial coder for this task. Within the same model, use the coder's effort or higher; if cross-model ordering is uncertain, use the coder profile or a clearly stronger profile. Use the host's supported planning workflow after approval, with the selected profile, to produce one advisory Markdown plan. Do not invent a planner service or replanning loop. If the host cannot use the selected profile for planning, report that limitation before execution. The coder must check and adapt the plan rather than follow it blindly.
- Explicitly decide whether to enable revision coder. Use it for bounded review-driven repairs when the fresh-thread switch is worthwhile; do not use a weak initial coder with a planned stronger rescue.
- Treat `runtime-only`, `C`, `A`, and `C+A` as examples, not a closed menu. Choose supported review counts from the task and preferences. Counts are upper bounds, not guaranteed literal call sequences.
- Decide coder, completion, and adversary sub-agent policies separately. For each enabled policy, choose concurrency, the default child profile, and the allowed provider/model/effort pool. Parents retain final judgment.
- Keep normal speed unless faster processing is justified by the user's preferences and supported by every affected profile. Do not silently map Bello's Fast setting to a provider's differently named feature. Cheap runtime is a separate switch with a configured triage route, not a freely selectable role in the project schema; see [CONFIG_SCHEMA.md](references/CONFIG_SCHEMA.md).

## Report

Return exactly one concise, human-readable setup in the user's language, following [OUTPUT_CONTRACT.md](references/OUTPUT_CONTRACT.md). Include the exact task path, planning decision, active roles and their provider/model/effort profiles, review schedule, sub-agent policies, speed, triage, and workspace handling.

Do not expose JSON, dormant fields, the selection process, alternatives, price/time/quality predictions, comparisons, citations, or validation narration. Do not assign task difficulty labels.

Construct and validate the complete config privately with `scripts/validate_config.py --file <private-config.json> --catalog <catalog.json> --project-root <root> --task-file <task>`. Temporary artifacts belong outside the project. If commands are forbidden, inspect the schema and perform the same checks manually. If compatibility cannot be established, return a concise blocker rather than omit a chosen feature. Do not claim that catalog presence proves remaining quota or that a recommendation guarantees a score.

## Apply only when asked

- Reconstruct the same active setup from the recommendation, validate it again with the current catalog, and do not silently reselect models or schedules.
- Run `scripts/inspect_config.py` with its version check only now, to preserve runtime-owned state and detect an active run. An uncertain apply guard needs a liveness recheck or clarification.
- If planning was approved, first create the single plan using the selected planner profile. It must be a regular Markdown file inside the project, distinct from the task, not named `AGENTS.md`, untracked, and absent from reachable Git history.
- Use Bello's supported configuration interface, not direct replacement of `.supervisor/config.json`. Preserve unrelated state, `start_over`, and protected paths. Destructive cleanup or unbounded budgets need explicit approval.
- Pass the approved plan with `--plan` when launching. Use the companion `bello-delegate` skill, when available, only after configuration and planning are complete; it must launch the selected settings, not choose new ones.
