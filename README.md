<h1 align="center">Bello</h1>

<p align="center">
  <strong>Your coding agent. Your choice of models, checks, and budget.</strong><br>
  Run a task with Codex, Claude Code, or API models. Add supervision, independent review, adversarial testing, or local log compression when you need them.
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
- [Build your own team](#build-your-own-team)
- [Results](#results)
- [Configuration](#configuration)
- [License](#license)

## TL;DR

Bello lets you choose a coding team, not just a coding model. Use Codex or
Claude Code subscriptions, or models from supported API providers. Mix models
across roles and subagents; the advisor can recommend a setup for your task.

- **Runtime** follows the live run, checks risky actions, and intervenes when the coder drifts.
- **Completion review** checks the implementation against the task and returns unfinished work.
- **Adversary** tries to break the solution with edge cases and unexpected inputs.
- **Log distiller** uses a locally fine-tuned ModernBERT to shorten tool output before the coder and its subagents read it.

All four are independently optional. Spend more on review when quality matters,
choose cheaper models when budget matters, or keep the pipeline light.
In our [tests](#results), Budget used **66.3% less** of the weekly Codex limit
than Raw GPT-5.6 Sol XHigh **without losing average completion quality**:
its mean score was **1.45% higher**.
Sol Ultra C+A improved the score by **26.4% relative**; distillation reduced
estimated usage by **22.0% on Astra** and **14.8% on Luna**.
These are different configurations and comparisons, not simultaneous guarantees.

## Install

**Requirements:**

- **Codex CLI** installed and authenticated. Bello drives `codex app-server`,
  and your Codex account provides the models.
- **Python 3.11+** and **git**.
- macOS, Linux, or a supported native Windows installation.

Verify your environment at any time with `bello doctor`.

**Option A: Codex plugin** (recommended if you work inside Codex):

```bash
pipx install bello
codex plugin marketplace add AlexeyKulaev/Bello-codex-marketplace --ref main
codex plugin add bello@bello-marketplace
```

Then open Codex in your project folder and ask it to run Bello on your task
file. The plugin checks for updates and launches the run for you.

**Option B: standalone CLI**

```bash
pipx install bello
bello doctor
```

Bello checks for updates at startup and offers to install them. Run
`bello update` to update explicitly.

## Quick start

After installing the plugin, open Codex in the project that contains `task.md`.
The full start can be a short conversation:

> **You:** Do you see the Bello plugin?
>
> **Codex:** Yes. I can inspect the task, recommend a configuration, and run it
> with Bello.
>
> **You:** Please recommend the best balance of price and quality for
> completing `task.md`.
>
> **Codex:** I recommend Configuration X for `task.md`. It offers the best
> balance of price, quality, and time for this task.
>
> **You:** Thanks. Please run `task.md` with Configuration X and keep me
> updated on what is happening.

Codex shows the resolved configuration before launch. Bello then runs the task
and writes `.supervisor/FINAL_REPORT.md` with the result, changed files, checks,
and remaining risks.

You can also ask a stronger model to prepare an advisory `PLAN.md`, then have a
less expensive Bello configuration execute it. The coder receives the plan as
guidance, while completion review and adversarial testing remain independent.

## Bello in 42 seconds

https://github.com/user-attachments/assets/f0324432-f616-45f6-beca-9bd8282f06ef

## Build your own team

The coder implements the task in a disposable workspace. Enabled reviewers send
confirmed issues back for repair, within the review budget you choose.
You can use one provider throughout or mix them:

| Role | One possible setup |
| --- | --- |
| Coder | GPT-5.6 Sol |
| Coder's subagents | Claude Sonnet 5, a GLM model, and GPT-5.6 Luna |
| Runtime supervisor | GPT-5.6 Terra |
| Completion reviewer | Claude Fable 5 |
| Adversary | GPT-6 Astra |
| Log distiller | Local ModernBERT, no paid model call |

This is an example, not a preset: select models available through your connected
accounts. Sonnet, GLM, and Luna can work as three concurrent subagents when their
profiles are allowed and the concurrency limit is at least three. Each role has
its own model and supported reasoning settings; completion and adversary can
also have their own subagents.

You can disable runtime without disabling review or distillation, or use an
adversary without completion review. Runtime off also turns off cheap runtime
triage. It means less protection: the filesystem sandbox remains, but the
runtime model no longer assesses actions or steers the coder.

The advisor reads your task and repository before suggesting a configuration.
You decide the priorities and approve the setup. Every setting remains editable
in `bello config`.

## Results

### Four models, with and without Bello

Two lines, four models, three ProgramBench tasks: Solar, Samtools, and rumdl.
Each point is the unweighted mean of the three reported task scores.
The 24 runs comprise 12 Raw and 12 Bello runs, not 24 pairs.

<picture>
  <source media="(max-width: 600px)" srcset="./docs/assets/readme-model-comparison-mobile.svg">
  <img src="./docs/assets/readme-model-comparison.svg" alt="Mean ProgramBench score: Luna Raw 32.75%, Bello 46.32%; Terra 31.75%, 41.24%; Sol 48.98%, 55.44%; Astra 59.70%, 65.68%." width="100%">
</picture>

Across these reported results, the mean rises from **43.29% to 52.17%**
(**+8.88 percentage points**). This comparison shows completion scores, not
lower cost or faster execution; those depend on the configuration.
[Browse the 24 solutions.](https://drive.google.com/drive/folders/1QkyIFUp4QwLSMtVYAOqbjSdmOIiaTnch)

### Log distiller: send less tool output to the coder

The distiller is a fine-tuned **ModernBERT-base with a small token-selection
head**, about **149 million parameters**. It selects original text rather than
writing a summary. Inference stays on your machine; logs are not sent to Hugging
Face or another paid model. Recognized task instructions, documentation reads,
and command help bypass compression.

These JSON Schema runs compare the same coder model with and without
distillation. Astra has runtime off in both arms; Luna's Bello arm also includes
runtime supervision.

<picture>
  <source media="(max-width: 600px)" srcset="./docs/assets/readme-distiller-mobile.svg">
  <img src="./docs/assets/readme-distiller.svg" alt="Estimated usage reduction: Astra XHigh 22.02%; Luna Max 14.78% including runtime, or 25.28% after subtracting recorded runtime cost. Scores: Astra 62.22% to 60.57%; Luna 56.75% to 55.45%." width="100%">
</picture>

| Coder | Runs per arm | Score, off → on | Mean solution time, off → on |
| --- | ---: | ---: | ---: |
| Astra XHigh | 3 | 62.22% → 60.57% | 33:04 → 42:10 |
| Luna Max | 6 | 56.75% → 55.45% | 1:41:53 → 1:22:38 |

**Luna's 14.8% includes runtime.** Subtracting the recorded runtime component
gives **25.3% lower coder cost**. That is accounting for these same runs, not a
separate runtime-off experiment. Astra used less while taking longer; Luna used
less and finished sooner. Both had a small score decrease.

[Download the model](https://huggingface.co/Makson179/bello-log-distiller)
· [Solutions and SHA-256 checksums](https://drive.google.com/drive/folders/1jDUSJ-PyRpWfDSHKDp6UEX5ZMoN0NmG5)

<details>
<summary>Evaluation notes</summary>

Usage reductions are estimates calculated from recorded input, cached-input,
and output tokens at API-equivalent rates; they are not direct readings of a
subscription's weekly counter. Local inference and server rental are excluded.
Times cover the solver, not the external verifier.

Astra uses the same native Codex app-server harness in both arms, with runtime,
completion review, adversary, and Fast off. The displayed comparison uses
control runs 2/3/4 and distiller runs 1/2/3. Two distiller scores come from
separate verification of preserved candidates after handoff failures. Hosts and
concurrency differed, so the time comparison is not an isolated latency test.

Luna pools two batches of three runs per arm; all six are included.
One solution needed a verifier-only executable-symlink repair, without rerunning
the coder. Scores use all 2,932 test IDs, with unrun tests counting as zero.
These are observations on one task, not guaranteed savings on every project.

</details>

### Efficient Budget: a cheaper team

<picture>
  <source media="(max-width: 600px)" srcset="./docs/assets/programbench-efficient-budget-quality-cost-mobile.svg">
  <img src="./docs/assets/programbench-efficient-budget-quality-cost.svg" alt="Budget versus Raw Sol XHigh: per-task mean scores and weekly-limit usage for Revive, JSON Schema, Lightning CSS, and Miller." width="100%">
</picture>

The Budget setup uses Luna XHigh for coding, completion review, and adversarial
testing, with Luna High / Medium for runtime and triage. Its baseline is
**Raw Sol XHigh**, not Raw Luna.

| Across four tasks, 12 runs per arm | Raw Sol XHigh | Bello Budget |
| --- | ---: | ---: |
| Mean score | 48.100% | **48.797%** |
| Weekly Codex limit used, all runs | 15.6297% | **5.2690%** |
| Mean solution time | **28:27** | 1:49:44 |

That is **66.3% less usage**, with a **1.45% relative score increase**, at the
expense of longer runs. Tasks: Revive, JSON Schema, Lightning CSS, and Miller.

[Solutions and checksums](https://drive.google.com/drive/folders/1W1Lm0U7gcb5rTa3DyXQH_6n6XbFXwB9c?usp=share_link)
· [Per-run results](https://github.com/Makson179/Bello/blob/32450a17456f3e4df804d5a45068d5cf1e4168ba/README.md#efficient-budget)

### Sol Ultra C+A: spend more on checking

<picture>
  <source media="(max-width: 600px)" srcset="./docs/assets/programbench-ca-performance-mobile.svg">
  <img src="./docs/assets/programbench-ca-performance.svg" alt="Sol Ultra C+A versus Raw Sol: mean score 53.53% to 67.67%, a 26.41% relative increase. Per-task scores and times for Solar, Samtools, and rumdl; total solution time 2:48:55 to 7:08:06." width="100%">
</picture>

With Sol Ultra, one completion-review stage and an adversarial pass raised the
mean score across Solar, Samtools, and rumdl from **53.53% to 67.67%**:
**+14.14 percentage points**, or **+26.41% relative**.
Total solution time across the three tasks rose from **2:48:55 to 7:08:06**.

[Run-level scores and times](./programbench_ca_run_info.csv)
· [Solutions](https://drive.google.com/drive/folders/1oWR5v3fziEZj1PkQ8xDyq5JBRCUPf5gV)

### Runtime-only: supervision without scheduled reviews

<picture>
  <source media="(max-width: 600px)" srcset="./docs/assets/runtime-only-custom-task-results-mobile.svg">
  <img src="./docs/assets/runtime-only-custom-task-results.svg" alt="Runtime-only versus Raw scores: Marl 32.91% to 37.91%; Slab 81.08% to 85.69%; Pinch 89.25% to 98.00%. Each task uses its own scoring criteria." width="100%">
</picture>

On three custom tasks with large, contradictory specifications, runtime-only
improved the score in each case. These tasks use their own scoring criteria.

| Task | Raw score → runtime-only | Raw time → runtime-only |
| --- | ---: | ---: |
| Marl | 32.91% → **37.91%** | 58:49 → 46:09 |
| Slab | 81.08% → **85.69%** | 57:26 → 1:04:11 |
| Pinch | 89.25% → **98.00%** | 40:09 → 43:34 |

[Task specifications and evidence](https://drive.google.com/drive/folders/1eLut349Wu_uxw59H6u87cuWNRqYb3x7x)

Older, deeper review schedules are kept in the
[4C+A+2C experiment archive](https://github.com/Makson179/Bello/blob/32450a17456f3e4df804d5a45068d5cf1e4168ba/README.md#3-4ca2c-maximum-effort).

## Configuration

```bash
bello config
bello runtime models
bello --task TASK.md
```

The editor saves settings in `.supervisor/config.json`. Choose models and
reasoning levels, permitted subagents and concurrency, review budgets, and the
four independent switches. CLI flags override the saved settings for one run.

For Claude Code or local distillation, install the optional dependencies:

```bash
pipx install 'Bello[claude,log-distiller]' --force
```

When enabled, the distiller downloads its pinned model once, then reuses the
local cache. With it off, Bello neither downloads nor loads the model.
Subscription Codex additionally needs the compatible native helper: automatic
setup is available on Apple Silicon; Linux needs a compatible build, and native
Windows distillation is not currently supported.

- [Providers, sign-in, independent switches, and local distillation](./docs/runtime.md)
- [Native Codex distiller setup and platform support](./docs/native-codex-selection.md)
- [Windows sandbox](./docs/windows.md)
- `bello --help` for command-line options; `bello doctor` to check your installation.

## License

Bello is released under the MIT License. See [LICENSE](./LICENSE).
The optional distiller model is distributed under Apache 2.0.

Contributions require signing the project [CLA](./CLA.md). A bot will prompt
you on your first pull request, and you only sign once.
