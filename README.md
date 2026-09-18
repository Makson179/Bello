<h1 align="center">Bello</h1>

<p align="center">
  <strong>Build a cheaper, more reliable, better coding team from the models you already have.</strong><br>
  Codex, Claude Code, OpenAI, Anthropic, OpenRouter, in any mix. Bello puts a supervisor, a reviewer, an adversary, and a local log distiller around the coder, each optional, each on its own model. In our tests, one of the configurations used 66% less Codex usage at the same quality.
</p>

<p align="center">
  <a href="https://github.com/Makson179/Bello/actions/workflows/tests.yml"><img alt="Tests" src="https://github.com/Makson179/Bello/actions/workflows/tests.yml/badge.svg"></a>
  <a href="https://www.python.org/downloads/"><img alt="Python 3.11+" src="https://img.shields.io/badge/python-3.11%2B-3776AB?style=flat-square&logo=python&logoColor=white"></a>
  <a href="./LICENSE"><img alt="License: MIT" src="https://img.shields.io/badge/license-MIT-0F766E?style=flat-square"></a>
</p>

<p align="center">
  <img src="./bello_pixel_intro.gif" alt="Bello pixel intro" width="100%">
</p>

## Contents

- [TL;DR](#tldr)
- [Install](#install)
- [Quick start](#quick-start)
- [Bello in 42 seconds](#bello-in-42-seconds)
- [How Bello runs a task](#how-bello-runs-a-task)
- [Results](#results)
- [Configuration](#configuration)
- [License](#license)

## TL;DR

Bello runs a coding task through a coder and four optional parts: a runtime
supervisor, a completion reviewer, an adversary, and a local log distiller.
The coder, the supervisor, the reviewer, and the adversary each have their own
model and reasoning effort, from a Codex or Claude Code subscription or from
an API provider. The four components can be enabled or disabled independently.

In our [tests](#results), the Efficient Budget configuration used 66.3% less
of the weekly Codex limit than Raw GPT-5.6 Sol XHigh without lowering average
quality, and its mean score was 1.45% higher. Sol Ultra C+A raised the mean
score over Raw Codex by 26.4%. The log distiller cut subscription usage by 22% on
Astra and by 14.8% on Luna.

## Install

Python 3.11 or newer, git, and macOS, Linux with the `bubblewrap` package, or
native 64-bit Windows 11 or Server 2022/2025 with the one-time sandbox
preparation from [docs/windows.md](./docs/windows.md).

```bash
pipx install bello
bello doctor
```

Then add the model sources you want, any one is enough:

- Codex subscription: install the Codex CLI and run `codex login`.
- Claude Code subscription: `pipx install 'bello[claude]' --force`, then
  `bello runtime login claude-code`. The extra bundles the official Agent SDK
  and its CLI.
- API providers (OpenAI, Anthropic, OpenRouter, and other Pi providers):
  Node.js 22.19 or newer, `bello runtime install` once, then
  `bello runtime login <provider>`.
- Log distiller: `pipx install 'bello[log-distiller]' --force`. The model
  (599 MB) downloads on the first run with the distiller on. With subscription
  Codex it also needs a patched Codex build (automatic on Apple Silicon,
  manual on Linux, not available on Windows), see
  [docs/native-codex-selection.md](./docs/native-codex-selection.md).

`bello doctor` shows what is ready, and `bello update` updates Bello. To run
Bello from inside your coding agent, add the plugin, which includes the
configuration advisor. For Codex:

```bash
codex plugin marketplace add AlexeyKulaev/Bello-codex-marketplace --ref main
codex plugin add bello@bello-marketplace
```

The same plugin has a Claude Code manifest in [plugins/bello](./plugins/bello).

## Quick start

After installing the plugin, open your coding agent in the project that
contains `task.md`. The full start can be a short conversation:

> **You:** Do you see the Bello plugin?
>
> **Agent:** Yes. I can inspect the task, recommend a configuration, and run it
> with Bello.
>
> **You:** Please recommend the best balance of price and quality for
> completing `task.md`.
>
> **Agent:** I recommend Configuration X for `task.md`. It offers the best
> balance of price, quality, and time for this task.
>
> **You:** Thanks. Please run `task.md` with Configuration X and keep me
> updated on what is happening.

The agent shows the resolved configuration before launch. Bello then runs the
task and writes `.supervisor/FINAL_REPORT.md` with the result, changed files,
checks, and remaining risks.

You can also ask a stronger model to prepare an advisory `PLAN.md`, then have a
less expensive Bello configuration execute it. The coder receives the plan as
guidance, while completion review and adversarial testing remain independent.

## Bello in 42 seconds

https://github.com/user-attachments/assets/f0324432-f616-45f6-beca-9bd8282f06ef


## How Bello runs a task

The coder implements the task in a disposable workspace and runs its own
checks. Bello then hands the final patch back to your project. Around the
coder, four parts can be switched on or off independently.

- **Runtime supervisor.** Follows the live run, judges risky commands before
  they run, redirects the coder when it drifts from the task, and can restart
  a failing run without losing the workspace. On by default.
- **Completion reviewer.** Starts with a fresh context, compares the result and
  its evidence with the task, and returns the work when something is missing
  or unproven.
- **Adversary.** Receives the finished result without the development history
  and tries to break it with edge cases, invalid input, and feature
  interactions. A separate check confirms its findings before they go back to
  the coder.
- **Log distiller.** A ModernBERT model we fine-tuned, running locally, that
  shortens tool output before the coder and its subagents read it. Reviewers
  get the undistilled output.

Confirmed problems go back to the coder, and the number of review and
adversary rounds is a setting. Roles can use different providers, including
API providers such as OpenAI, Anthropic, and OpenRouter, and the coder, the
reviewer, and the adversary can also delegate work to subagents with their own
models. For example, when these models are available in your accounts:

| Role | Model |
| --- | --- |
| Coder | GPT-5.6 Sol |
| Coder subagents, three in parallel | Claude Sonnet, GLM, GPT-5.6 Luna |
| Runtime supervisor | GPT-5.6 Terra |
| Completion reviewer | Claude Fable |
| Adversary | GPT-6 Astra |
| Log distiller | ModernBERT on your machine |

You do not have to pick all of this by hand. The advisor in the Codex and
Claude Code plugins reads the task and the repository and recommends one
complete setup for the priority you name. Every setting is also in
`bello config`.

## Results

Scores are ProgramBench completion scores unless a task has its own evaluator.
Raw means a model run through Codex alone, without Bello.

### Four models, raw and with Bello

Three ProgramBench tasks (Solar, Samtools, and Rumdl), four models, each run
raw and through Bello: 24 runs in total. Sol ran at `xhigh` here.

<picture>
  <source media="(max-width: 600px)" srcset="./docs/assets/readme-model-comparison-mobile.svg">
  <img src="./docs/assets/readme-model-comparison.svg" alt="Mean ProgramBench score per model: Luna raw 32.75%, Bello 46.32%; Terra 31.75%, 41.24%; Sol 48.98%, 55.44%; Astra 59.70%, 65.68%" width="100%">
</picture>

Bello scored higher with every model. The gap is 13.6 points on Luna, 9.5 on
Terra, 6.5 on Sol, and 6.0 on Astra.
[Solutions for all 24 runs.](https://drive.google.com/drive/folders/1QkyIFUp4QwLSMtVYAOqbjSdmOIiaTnch)

### Efficient Budget: less usage at the same quality

Efficient Budget uses GPT-5.6 Luna at `xhigh` for the coder, the completion
reviewer, and the adversary, and Luna at `high` for runtime supervision with
cheap triage on. It allows one completion return before one adversary pass.
The baseline is Raw GPT-5.6 Sol XHigh.

Across four tasks with three runs per task and system, Budget used 5.27% of a
weekly Codex limit against 15.63% for Raw, which is 66.3% less. Its mean score
was 48.80% against 48.10%, 1.45% higher. Runs took longer: 1:49:44 on average
against 28:27.

| Task | Raw score | Budget score | Change | Raw weekly limit | Budget weekly limit | Raw time | Budget time |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Revive | 40.990% | 45.530% | +11.08% | 3.2757% | 1.0555% | 24:52 | 1:23:22 |
| JSONSchema | 56.821% | 54.673% | -3.78% | 3.0069% | 0.8138% | 23:34 | 1:20:43 |
| LightningCSS | 60.750% | 60.302% | -0.74% | 6.0281% | 2.2569% | 40:39 | 2:58:52 |
| Miller | 33.839% | 34.684% | +2.50% | 3.3190% | 1.1428% | 24:44 | 1:36:00 |
| All 12 + 12 | 48.100% | 48.797% | +1.45% | 15.6297% | 5.2690% | 28:27 | 1:49:44 |

*Scores and times are means over three runs; weekly limit is the sum.*

<picture>
  <source media="(max-width: 600px)" srcset="./docs/assets/programbench-efficient-budget-quality-cost-mobile.svg">
  <img src="./docs/assets/programbench-efficient-budget-quality-cost.svg" alt="Efficient Budget quality and weekly limit use compared with Raw GPT-5.6 Sol XHigh" width="100%">
</picture>

[Solutions and checksums.](https://drive.google.com/drive/folders/1W1Lm0U7gcb5rTa3DyXQH_6n6XbFXwB9c?usp=share_link)
[Per-run rows.](https://github.com/Makson179/Bello/blob/v0.5.2/README.md#efficient-budget)

### Sol Ultra C+A: higher quality for more time

With GPT-5.6 Sol at `ultra`, one completion review and one adversary pass
raised the mean completion score on Solar, Samtools, and Rumdl from 53.53% to
67.67%, which is 26.41% higher than Raw Codex. Total time across the three
tasks rose from 2:48:55 to 7:08:06.

| Task | Raw completion | C+A completion | Change | Raw time | C+A time |
| --- | ---: | ---: | ---: | ---: | ---: |
| Solar | 53.13% | 59.00% | +11.05% | 00:32:33 | 02:17:45 |
| Samtools | 51.86% | 63.00% | +21.48% | 00:36:17 | 02:14:40 |
| Rumdl | 55.60% | 81.00% | +45.68% | 01:40:05 | 02:35:41 |
| Mean / total time | 53.53% | 67.67% | +26.41% | 02:48:55 | 07:08:06 |

<picture>
  <source media="(max-width: 600px)" srcset="./docs/assets/programbench-ca-performance-mobile.svg">
  <img src="./docs/assets/programbench-ca-performance.svg" alt="C+A completion and runtime compared with Raw Codex" width="100%">
</picture>

[Run-level scores and times.](./programbench_ca_run_info.csv)
[Solutions.](https://drive.google.com/drive/folders/1oWR5v3fziEZj1PkQ8xDyq5JBRCUPf5gV)

### Runtime-only: supervision alone

With only the runtime supervisor on, Bello scored higher on three custom tasks
built from long specifications with contradictions and late corrections, in
about the same time as Raw Codex. Each task has its own evaluator on a 0 to
100 scale. On the shorter ProgramBench tasks (Solar, Samtools, and Rumdl) the
average gain was about 2%.

| Task | Raw score | Runtime-only score | Change | Raw time | Runtime-only time |
| --- | ---: | ---: | ---: | ---: | ---: |
| Marl | 32.91% | 37.91% | +15.19% | 00:58:49 | 00:46:09 |
| Slab | 81.08% | 85.69% | +5.69% | 00:57:26 | 01:04:11 |
| Pinch | 89.25% | 98.00% | +9.80% | 00:40:09 | 00:43:34 |

<picture>
  <source media="(max-width: 600px)" srcset="./docs/assets/runtime-only-custom-task-results-mobile.svg">
  <img src="./docs/assets/runtime-only-custom-task-results.svg" alt="Runtime-only results on large tasks with contradictory specifications" width="100%">
</picture>

[Task briefs, tests, and evaluator outputs.](https://drive.google.com/drive/folders/1eLut349Wu_uxw59H6u87cuWNRqYb3x7x)

### Log distiller: less tool output for the coder

The distiller is a ModernBERT-base encoder with a small token-selection head,
about 149 million parameters. In the coder pipeline, each tool call carries a
short focus written by the coder, what it wants from the result, and the
distiller uses that focus to decide which parts of the output to keep and
which to drop. It keeps original text and writes no summary. It runs on your
CPU, so logs stay on your machine and no paid model call is added. The task
file, reads of instruction files such as README or AGENTS.md, and command help
pass through unchanged.

We measured it on the JSON Schema task with the same coder model, with and
without distillation, on a weaker model (GPT-5.6 Luna) and on a frontier model
(GPT-6 Astra). Usage fell with both, so the selector works for weak and strong
coders alike.

<picture>
  <source media="(max-width: 600px)" srcset="./docs/assets/readme-distiller-mobile.svg">
  <img src="./docs/assets/readme-distiller.svg" alt="Cost versus quality: each model's Raw cost is normalized to 100. Astra XHigh changes from cost 100 and score 62.22% to cost 77.98 and score 60.57%. Luna Max changes from cost 100 and score 56.75% to cost 85.22 and score 55.45%, including runtime." width="100%">
</picture>

| Coder | Runs per arm | Usage | Score, off to on | Mean solution time, off to on |
| --- | ---: | ---: | ---: | ---: |
| Astra XHigh, runtime off | 3 | -22.0% | 62.22% to 60.57% | 33:04 to 42:10 |
| Luna Max, runtime on | 6 | -14.8% | 56.75% to 55.45% | 1:41:53 to 1:22:38 |

Luna's 14.8% includes the runtime supervisor's usage in the Bello arm. After
subtracting the recorded runtime cost, the coder alone used 25.3% less. Scores were 1.3 to 1.7 points lower with the
distiller on.

[Model on Hugging Face.](https://huggingface.co/Makson179/bello-log-distiller)
[Solutions and checksums.](https://drive.google.com/drive/folders/1jDUSJ-PyRpWfDSHKDp6UEX5ZMoN0NmG5)

Results for the older 4C+A+2C schedule (four completion reviews before the
adversary and two after) are archived in the
[0.5.2 README](https://github.com/Makson179/Bello/blob/v0.5.2/README.md#3-4ca2c-maximum-effort).

## Configuration

```bash
bello config                      # interactive editor, saves .supervisor/config.json
bello runtime models              # models and reasoning efforts your accounts can use
bello --task TASK.md --adversary  # run flags override the saved settings for one run
```

The editor covers the model and reasoning effort of each role, subagent pools
and concurrency, review budgets, and the four switches. `bello --help` lists
the run flags.

- [Providers, sign-in, switches, and the distiller](./docs/runtime.md)
- [Native Codex distiller setup](./docs/native-codex-selection.md)
- [Windows sandbox](./docs/windows.md)

## License

Bello is released under the MIT License. See [LICENSE](./LICENSE). The
distiller model is distributed separately under Apache 2.0.

Contributions require signing the project [CLA](./CLA.md). A bot will prompt
you on your first pull request, and you only sign once.
