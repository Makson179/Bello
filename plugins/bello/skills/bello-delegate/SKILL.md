---
name: bello-delegate
description: Launch an installed Bello supervisor in the background for a repository coding task, or inspect an existing Bello run. Use when the user asks to delegate implementation to Bello, run Bello, continue monitoring Bello, or report Bello status. Do not use merely to advise on or redesign Bello configuration.
---

# Bello Delegate

Use Bello as the coding workflow and treat the current Codex or Claude Code agent as its launcher and observer. The frontend agent and its model do not choose Bello's coder, runtime, completion, or adversary models.

## Locate the helper

Set `SKILL_DIR` to the directory containing this `SKILL.md`. For a Claude Code plugin installation that path is `${CLAUDE_PLUGIN_ROOT}/skills/bello-delegate`; in other hosts use the loaded skill's own directory. Run the helper with the host's Python 3 executable:

```text
python <SKILL_DIR>/scripts/bello_delegate.py status --project <repository>
```

The helper emits JSON. Use its absolute `project` and `runDirectory` fields in later calls.

## Start a run

Run from the target repository, or pass it with `--project`:

```text
python <SKILL_DIR>/scripts/bello_delegate.py start --project <repository>
```

With no task or plan options, the launcher invokes `bello` with no arguments. Bello then uses the project's existing `.supervisor/config.json` and its normal task discovery. Do not infer or pass any model, provider, effort, service-tier, runtime, distiller, review, cleanup, or restart option from the frontend session.

When the user supplies or approves a task file or plan file, append the corresponding option. This includes a plan created in a separately approved planning pass; do not invent or add a planning pass yourself:

```text
python <SKILL_DIR>/scripts/bello_delegate.py start --project <repository> --task TASK.md --plan PLAN.md
```

The launcher validates both files as ordinary files inside the repository. It rejects a duplicate active launch and starts Bello in the background. It never installs, updates, authenticates, configures, or publishes Bello. Do not run the Bello configuration advisor as part of this workflow. Run `bello config` only if the user separately asks to configure Bello.

Runtime supervision, completion review, adversary, and log distiller are independent saved switches. Runtime-off makes no runtime/triage model calls; C/A still follow their own settings. It reduces live protection and allows network inside the filesystem sandbox, not automatic outside-sandbox grants. Distiller is off by default. An approved enabled run downloads Bello's pinned default model once (about 599 MB) and reuses its cache, unless a compatible local bundle is selected. The optional `log-distiller` dependencies must be installed; native Codex also needs the compatible selection-hook executable. On supported platforms Bello prepares that pinned helper automatically in its private cache, without changing global Codex; other platforms require an explicit compatible build. Report setup failures, do not silently install dependencies, change providers, or disable the selected feature. CPU selection has a 300-second request budget including queue time; do not promise savings. If changed runtime/distiller settings conflict with existing threads, report that a fresh run is required; do not silently restart or rewrite the saved policy.

## Monitor and report

Poll `status` at reasonable intervals while the helper reports `launching` or `running`. Treat process-liveness fields as corroboration, not proof: use `.supervisor/config.json`, `PROGRESS.md`, `DECISIONS.md`, and `FINAL_REPORT.md` as the durable source of run state. The helper also returns bounded stdout, stderr, and Git-status summaries.

Stop polling when the launcher reports `exited`, `launch_failed`, or `stale`. On success, report the final Bello status, concise result, changed files, and validations from `FINAL_REPORT.md`. On failure or staleness, report the diagnostic without silently starting a replacement run. Do not edit Bello's `.supervisor` state files or take over the delegated coding work unless the user explicitly asks.
