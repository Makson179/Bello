<h1 align="center">Bello</h1>

<p align="center">
  <strong>Spend less on coding tasks without giving up completion quality.</strong><br>
  Bello assigns models to coding, runtime supervision, completion review, and adversarial testing. Across 12 + 12 matched ProgramBench runs, <a href="#results">one of the tested configurations</a> used 66.3% less of a weekly Codex limit than Raw GPT-5.6 Sol XHigh while scoring 1.45% higher on average. Settings that prioritize quality can raise completion further. <br>
</p>

<p align="center">
  <a href="https://github.com/Makson179/Bello/actions/workflows/tests.yml"><img alt="Tests" src="https://github.com/Makson179/Bello/actions/workflows/tests.yml/badge.svg"></a>
  <a href="https://www.python.org/downloads/"><img alt="Python 3.11+" src="https://img.shields.io/badge/python-3.11%2B-3776AB?style=flat-square&logo=python&logoColor=white"></a>
  <a href="./LICENSE"><img alt="License: MIT" src="https://img.shields.io/badge/license-MIT-0F766E?style=flat-square"></a>
  <img alt="Transport: Codex app-server JSON-RPC" src="https://img.shields.io/badge/transport-codex%20app--server-334155?style=flat-square">
  <img alt="Approvals: fail closed" src="https://img.shields.io/badge/approvals-fail--closed-B91C1C?style=flat-square">
</p>

<p align="center">
  <img src="./bello_pixel_intro.gif" alt="Bello pixel intro" width="100%">
</p>

## Contents

- [TL;DR](#tldr)
- [Install](#install)
- [Quick start](#quick-start)
- [Bello in 42 seconds](#bello-in-42-seconds)
- [How Bello solves tasks](#how-bello-solves-tasks)
- [Choose your configuration](#choose-your-configuration)
- [Results](#results)
- [Configuration](#configuration)
- [Command reference](#command-reference)
- [License](#license)

---

## TL;DR

Bello turns a Codex run into a configurable, supervised engineering pipeline.
Instead of asking one model to implement, monitor, review, and validate its own
work, Bello separates those responsibilities between a coder, a live runtime
supervisor, an independent completion reviewer, and an adversary. Each role can
use its own model and reasoning level. The same system can therefore reduce the
cost of a complete run, push completion quality beyond Raw Codex, and protect an
autonomous coding session from hallucinations, task drift, and harmful actions.

Cost is the clearest measured advantage in one of the configurations tested in
[Results](#results). Across four ProgramBench tasks, Raw GPT-5.6 Sol XHigh
consumed **2.966 times** as much of the weekly Codex limit, while Bello scored
**1.45% higher** on average. When quality takes priority over cost, the deeper
configuration scored **36.4% higher** than Raw Codex across nine matched runs.

Protection remains active even in the lightest configuration. The coder works
inside a disposable sandbox while the runtime supervisor follows the live run,
redirects hallucinated or off-task work, blocks dangerous actions before they
reach the project or production systems, and can restart a failing generation
without losing the workspace. On three large custom tasks built from
deliberately messy and contradictory specifications, `runtime-only` scored about
**9% higher** than Raw Codex at about the same cost and time. Those are the
conditions where long autonomous runs are especially prone to drift and
invented assumptions.

These outcomes are not tied to fixed presets. Every role can use a different
model, reasoning level, review budget, and service tier. One configuration can
reserve expensive reasoning for decisive reviews, another can spend more to
maximize quality, and another can use faster profiles to reduce time
while preserving a similar balance of price and quality. The Codex plugin can
inspect the task and recommend a complete configuration around the user's
priorities.

Install the plugin or standalone command in [Install](#install), then provide a
task file as shown in [Quick start](#quick-start). Bello runs the selected
configuration and writes `.supervisor/FINAL_REPORT.md` with the status, changed
files, checks, and remaining risks.

---

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

## How Bello solves tasks

Bello separates implementation, live safety, review, and attack into roles that
can use different models and reasoning levels.

1. The **coder** works inside a disposable sandbox. It reads the task, changes
   the project, and runs checks without receiving direct access to the original
   workspace.
2. The **runtime supervisor** watches the coder while it works. It catches
   hallucinations and task drift, blocks dangerous actions before they happen,
   protects project and production resources, and can restart a failing run
   without discarding the current workspace.
3. An optional **completion reviewer** reads the task, code, and diff with a
   fresh context. It checks whether the requested behavior is actually present
   and supported by evidence. Confirmed gaps go back to the coder.
4. An optional **adversary** receives the finished artifact without the
   development history and tries to break it through edge cases, invalid input,
   and feature interactions. A separate controller checks its findings before
   they reach the coder.
5. Bello returns confirmed problems to the coder and repeats only the stages
   allowed by the selected configuration.

The complete loop is:

**build → supervise → review → attack → repair → accept**

Every part is configurable. You can choose the model and reasoning level for
each role, the number and order of review and adversary passes, whether findings
move to a fresh revision coder, whether roles may use subagents, and whether to
use the faster service tier. A run can contain only runtime protection, one
review, several review and adversary rounds, or any supported combination. The
configuration advisor can inspect the task and recommend a setup around cost,
quality, and time.

## Choose your configuration

Bello is not limited to a small set of preset modes. You can combine its roles,
models, reasoning levels, and review budgets around the task. The configurations
below are examples of what that flexibility can produce.

| Example | Configuration | What it prioritizes | Measured result |
| --- | --- | --- | --- |
| Runtime protection | `runtime-only` | Safety with almost no added cost. The live supervisor catches hallucinations and drift, blocks harmful actions, and protects project and production resources. | About the same cost and time as Raw Codex. Scores were about 9% higher on three large tasks with deliberately messy, contradictory specifications and about 2% higher on the shorter ProgramBench tasks. |
| Efficient Budget | Luna-based `C+A` | Lower total cost without losing average quality. | Raw GPT-5.6 Sol XHigh used **2.966 times** as much of the weekly limit. Bello scored **1.45% higher** on average across four tasks. |
| Quality C+A | GPT-5.6 Sol `ultra` with `C+A` | A strong completion review and adversarial pass when quality matters more than cost. | Macro completion increased from **53.53% to 67.67%**, making Bello **26.41% better**. |
| Maximum quality experiment | `4C+A+2C` | The highest quality Bello can pursue with repeated review before and after an attack. | Bello scored higher in all nine matched runs and improved completion by **36.4%** on average. This is an expensive, long-running experiment for rare cases, not a default recommendation. |

Here, `C` means an independent completion review and `A` means an adversarial
pass. Runtime supervision remains active in every example. The same `C+A`
schedule can be inexpensive or quality-focused because each role can use a
different model and reasoning level.

You can also use one review without an adversary, several reviews, reviews after
an adversary, repeated attacks, or any supported combination. Review counts are
upper limits, so Bello can accept early when no further work is needed. For a
faster run, use quicker reasoning profiles on the serial roles and reserve the
strongest models for the decisions that need them. The coder, completion
reviewer, and adversary can each use bounded subagents for independent work.

You do not have to choose manually. The plugin's configuration advisor inspects
the task and relevant workspace files, presents a few concrete options for cost,
quality, and time, and applies the selected configuration when you ask it
to. Every field remains available in `bello config` and in the
[Configuration section](#configuration).

## Results

### 1. `runtime-only`, low-cost protection

On the three ProgramBench tasks, Solar, Samtools, and Rumdl, `runtime-only`
improved average completion by approximately **2%** over Raw Codex. The larger
benefit is risk control. A fresh supervisor can catch a dangerous action or a bad
trajectory before it becomes an unrecoverable final result, without the cost of
scheduled completion-review and adversary rounds.

We also tested `runtime-only` on
[three custom tasks](https://drive.google.com/drive/u/1/folders/1eLut349Wu_uxw59H6u87cuWNRqYb3x7x)
built from large, deliberately messy specifications with contradictions and
late corrections. They stress the kind of long autonomous run in which an agent
can lose requirements, follow an outdated instruction, or invent assumptions.

| Task | Raw Codex score | `runtime-only` score | Change | Raw Codex time | `runtime-only` time |
| --- | ---: | ---: | ---: | ---: | ---: |
| Marl (weighted spec completeness) | 32.91% | **37.91%** | **+15.19%** | 00:58:49 | 00:46:09 |
| Slab | 81.08% | **85.69%** | **+5.69%** | 00:57:26 | 01:04:11 |
| Pinch | 89.25% | **98.00%** | **+9.80%** | 00:40:09 | 00:43:34 |

![Runtime-only results on large tasks with deliberately messy specifications](./docs/assets/runtime-only-custom-task-results.svg)

*Figure R1. Comparable 0 to 100 evaluator scores for Marl, Slab, and Pinch. The
scores are separate task-specific measures, not components of a pooled
benchmark.*

The [linked](https://drive.google.com/drive/u/1/folders/1eLut349Wu_uxw59H6u87cuWNRqYb3x7x)
folder contains the complete task briefs, tests, evaluator outputs, and result
artifacts.

### 2. `C+A`, a balance of cost and quality

#### Efficient Budget

Efficient Budget uses GPT-5.6 Luna at `xhigh` for the coder, completion
reviewer, and adversary, Luna at `high` for runtime supervision, and Luna at
`medium` for cheap runtime triage. It allows one completion return before one
adversarial pass and no completion pass after it.

Across 12 runs per system, Bello consumed **5.2690%** of a weekly Codex limit,
compared with **15.6297%** for Raw GPT-5.6 Sol XHigh. Raw used **2.966 times** as
much of the limit. Bello's mean score was **48.797%**, compared with **48.100%**
for Raw, making Bello **1.45% better**. Mean solution time was
**1:49:44** for Bello and **28:27** for Raw.

| Task | System | Run | Score | Tests | Solution time | Weekly limit |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| Revive | Raw XHigh | 1 | 40.715% | 296/727 | 22:17 | 0.9526% |
| Revive | Raw XHigh | 2 | 36.726% | 267/727 | 25:30 | 0.9599% |
| Revive | Raw XHigh | 3 | 45.530% | 331/727 | 26:50 | 1.3632% |
| Revive | Bello | 1 | 49.519% | 360/727 | 1:47:11 | 0.4698% |
| Revive | Bello | 2 | 44.017% | 320/727 | 55:57 | 0.2413% |
| Revive | Bello | 3 | 43.054% | 313/727 | 1:26:59 | 0.3443% |
| JSONSchema | Raw XHigh | 1 | 57.299% | 1680/2932 | 20:07 | 0.7949% |
| JSONSchema | Raw XHigh | 2 | 56.685% | 1662/2932 | 24:40 | 1.0861% |
| JSONSchema | Raw XHigh | 3 | 56.480% | 1656/2932 | 25:54 | 1.1260% |
| JSONSchema | Bello | 1 | 53.104% | 1557/2932 | 1:05:52 | 0.2060% |
| JSONSchema | Bello | 2 | 55.730% | 1634/2932 | 1:20:50 | 0.2730% |
| JSONSchema | Bello | 3 | 55.184% | 1618/2932 | 1:35:27 | 0.3348% |
| LightningCSS | Raw XHigh | 1 | 59.689% | 1688/2828 | 29:02 | 1.0299% |
| LightningCSS | Raw XHigh | 2 | 61.139% | 1729/2828 | 42:22 | 2.3325% |
| LightningCSS | Raw XHigh | 3 | 61.421% | 1737/2828 | 50:34 | 2.6656% |
| LightningCSS | Bello | 1 | 58.098% | 1643/2828 | 1:38:07 | 0.3695% |
| LightningCSS | Bello | 2 | 62.023% | 1754/2828 | 4:05:16 | 1.0348% |
| LightningCSS | Bello | 3 | 60.785% | 1719/2828 | 3:13:12 | 0.8526% |
| Miller | Raw XHigh | 1 | 31.598% | 4625/14637 | 21:52 | 0.8872% |
| Miller | Raw XHigh | 2 | 36.606% | 5358/14637 | 27:25 | 1.2346% |
| Miller | Raw XHigh | 3 | 33.313% | 4876/14637 | 24:54 | 1.1972% |
| Miller | Bello | 1 | 36.503% | 5343/14637 | 1:09:41 | 0.2823% |
| Miller | Bello | 2 | 34.488% | 5048/14637 | 1:48:28 | 0.4200% |
| Miller | Bello | 3 | 33.060% | 4839/14637 | 1:49:50 | 0.4405% |

![Efficient Budget quality and weekly limit use compared with Raw GPT-5.6 Sol XHigh](./docs/assets/programbench-efficient-budget-quality-cost.svg)

*Figure C1. Score and weekly Codex limit used across three runs per task.*

| Task | Raw XHigh score | Bello score | Quality change | Raw weekly limit n=3 | Bello weekly limit n=3 | Cheaper | Bello as share of Raw | Time Raw | Time Bello | Slower |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Revive | 40.990% | 45.530% | +11.08% | 3.2757% | 1.0555% | 3.104× | 32.221% | 24:52 | 1:23:22 | 3.352× |
| JSONSchema | 56.821% | 54.673% | −3.78% | 3.0069% | 0.8138% | 3.695× | 27.064% | 23:34 | 1:20:43 | 3.426× |
| LightningCSS | 60.750% | 60.302% | −0.74% | 6.0281% | 2.2569% | 2.671× | 37.440% | 40:39 | 2:58:52 | 4.399× |
| Miller | 33.839% | 34.684% | +2.50% | 3.3190% | 1.1428% | 2.904× | 34.432% | 24:44 | 1:36:00 | 3.882× |
| **All 12 + 12** | **48.100%** | **48.797%** | **+1.45%** | **15.6297%** | **5.2690%** | **2.966×** | **33.711%** | **28:27** | **1:49:44** | **3.857×** |

#### Settings that prioritize quality

With GPT-5.6 Sol at `ultra`, C+A raised the unweighted macro completion score
from **53.53% to 67.67%**, making Bello **26.41% better**.
The three-task runtime was **07:08:06**, compared with **02:48:55** for Raw
Codex. The corresponding rows are available in the
[C+A run-level data](./programbench_ca_run_info.csv).
The corresponding Bello solutions are available in the
[C+A solution artifacts folder](https://drive.google.com/drive/u/1/folders/1oWR5v3fziEZj1PkQ8xDyq5JBRCUPf5gV).

| Task | Raw Codex completion | C+A completion | Change | Raw Codex time | C+A time |
| --- | ---: | ---: | ---: | ---: | ---: |
| Solar | 53.13% | **59.00%** | +11.05% | 00:32:33 | 02:17:45 |
| Samtools | 51.86% | **63.00%** | +21.48% | 00:36:17 | 02:14:40 |
| Rumdl | 55.60% | **81.00%** | +45.68% | 01:40:05 | 02:35:41 |
| **Macro mean / total time** | 53.53% | **67.67%** | **+26.41%** | **02:48:55** | **07:08:06** |

![C+A completion and runtime compared with Raw Codex](./docs/assets/programbench-ca-performance.svg)

*Figure C2. ProgramBench completion and runtime for the three matched GPT-5.6
Sol `ultra` task configurations.*

### 3. `4C+A+2C`, maximum effort

#### Key findings

- Across all three tasks and all model and effort settings, Bello achieved the
  higher completion score in **9 of 9 matched configurations**. The overall
  unweighted mean increased from **44.87% to 61.21%**, making Bello **36.40%
  better**.
- With GPT-5.6 Sol, Bello achieved the higher completion score in **6 of 6
  matched configurations**. The unweighted mean increased from **48.92% to
  67.04%**, making Bello **37.06% better**.
- In the complete GPT-5.6 Sol `ultra` comparison, every task improved by
  **34.20% to 44.23%**, and the macro average increased from **53.53% to
  74.03%**, a **38.30% improvement**.
- With GPT-5.5 `xhigh`, Bello scored higher on all three tasks, and the macro
  average increased from **36.79% to 49.53%**, a **34.64% improvement**.

#### Evaluation protocol

We evaluated Bello on three ProgramBench tasks: **Solar**, **Samtools**, and
**Rumdl**. Raw Codex and Bello were observed on every task with GPT-5.6 Sol in
both `ultra` and `xhigh` modes and with GPT-5.5 in `xhigh` mode. We report the
completion score recorded in the `completion_pct` field and time from the
`runtime` field of the [run-level data](./programbench_run_info.csv).
Completion scores are rounded to two decimal places.
Runtime was not held constant, so the comparison is not compute matched.
The final solution patches for all nine reported Bello runs, together with
SHA-256 checksums, are available in the
[public evaluation artifacts folder](https://drive.google.com/drive/folders/1MSyxidKXeQz7DA0gKn6KJtcWmefFu2-D?usp=share_link).

#### GPT-5.6 Sol

##### `ultra`

| Task | Raw Codex completion | Bello completion | Change | Raw Codex time | Bello time |
| --- | ---: | ---: | ---: | ---: | ---: |
| Solar | 53.13% | **71.30%** | +34.20% | 00:32:33 | 07:39:17 |
| Samtools | 51.86% | **70.60%** | +36.14% | 00:36:17 | 19:25:22 |
| Rumdl | 55.60% | **80.19%** | +44.23% | 01:40:05 | 07:44:12 |
| **Macro mean / total time** | 53.53% | **74.03%** | **+38.30%** | **02:48:55** | **34:48:51** |

*Bold completion values indicate the higher observed score within each matched
row.*

Across the three matched `ultra` runs, Bello improved completion by 34.20% to
44.23% on every task. The unweighted macro average rose from 53.53% to 74.03%,
a 38.30% improvement.

![GPT-5.6 Sol ultra completion improvements](./docs/assets/programbench-5-6-ultra-matched-differences.svg)

*Figure 1a. Completion improvements over Raw Codex for the three GPT-5.6 Sol
`ultra` configurations. The diamond shows the change in the unweighted macro
mean (38.30%). Uncertainty intervals are not shown because each configuration
has one observation.*

##### `xhigh`

| Task | Raw Codex completion | Bello completion | Change | Raw Codex time | Bello time |
| --- | ---: | ---: | ---: | ---: | ---: |
| Solar | 46.61% | **66.50%** | +42.67% | 00:16:58 | 04:26:04 |
| Samtools | 38.11% | **51.93%** | +36.26% | 00:28:48 | 05:39:13 |
| Rumdl | 48.19% | **61.74%** | +28.12% | 00:31:57 | 03:53:35 |
| **Macro mean / total time** | 44.30% | **60.06%** | **+35.56%** | **01:17:43** | **13:58:52** |

*Bold completion values indicate the higher observed score within each matched
row.*

All three `xhigh` tasks improved. The gains ranged from 28.12% to 42.67%, and
the unweighted macro average increased from 44.30% to 60.06%, a 35.56%
improvement.

![GPT-5.6 Sol xhigh completion improvements](./docs/assets/programbench-5-6-xhigh-matched-differences.svg)

*Figure 1b. Completion improvements over Raw Codex for the three GPT-5.6 Sol
`xhigh` configurations. The diamond shows the change in the unweighted macro
mean (35.56%). Uncertainty intervals are not shown because each configuration
has one observation.*

#### GPT-5.5

##### `xhigh`

| Task | Raw Codex completion | Bello completion | Change | Raw Codex time | Bello time |
| --- | ---: | ---: | ---: | ---: | ---: |
| Solar | 43.78% | **53.39%** | +21.95% | 00:16:27 | 01:29:35 |
| Samtools | 20.28% | **44.21%** | +118.00% | 00:16:28 | 02:30:01 |
| Rumdl | 46.30% | **50.99%** | +10.13% | 00:26:03 | 03:30:01 |
| **Macro mean / total time** | 36.79% | **49.53%** | **+34.64%** | **00:58:58** | **07:29:37** |

*Bold completion values indicate the higher observed score within each matched
row.*

Bello's score was higher on all three tasks. The improvements ranged from
10.13% to 118.00%, and the unweighted macro average increased from 36.79% to
49.53%, a 34.64% improvement.

#### Cross-task completion summary

![Cross-task completion scores for all three model and effort comparisons](./docs/assets/programbench-cross-task-completion.svg)

*Figure 2. Cross-task completion summary on a common 0% to 100% scale. Panels
(a), (b), and (c) show the matched GPT-5.6 Sol `ultra`, GPT-5.6 Sol `xhigh`,
and GPT-5.5 `xhigh` comparisons. The unweighted macro improvements are 38.30%,
35.56%, and 34.64%, respectively.*

#### Task-level configuration profiles

The following panels compare all three complete three-task configurations:
GPT-5.5 `xhigh`, GPT-5.6 Sol `xhigh`, and GPT-5.6 Sol `ultra`. Each panel
contains exactly six bars, one for Raw Codex and one for Bello in each model and
effort setting, ordered by increasing completion score. Bello precedes Raw Codex
when scores are tied. Ordering is descriptive and does not imply compute
equivalence.

![Solar configuration profile](./docs/assets/programbench-solar.svg)

*Figure 3a. Solar completion scores for the six configurations, sorted from
lowest to highest. The two formerly tied values are shown at their available
precision: Codex GPT-5.6 Sol `ultra` at 53.13% and Bello GPT-5.5 `xhigh` at
53.39%.*

![Samtools configuration profile](./docs/assets/programbench-samtools.svg)

*Figure 3b. Samtools completion scores for the six configurations, sorted from
lowest to highest.*

![Rumdl configuration profile](./docs/assets/programbench-rumdl.svg)

*Figure 3c. Rumdl completion scores for the six configurations, sorted from
lowest to highest.*

## Configuration

Open the interactive editor from your project folder:

```bash
bello config
```

The editor saves `.supervisor/config.json` for the project and shows only the
settings relevant to the active pipeline. Choose the model and reasoning level
for each role, enable completion review or adversarial testing, set review
budgets, and optionally configure a revision coder, subagents, or the Fast tier.

CLI flags override matching values for one run without rewriting the saved
configuration. The full setting list is below.

| Setting | Default | What it does |
| --- | --- | --- |
| `task` | absent | Default task file for this folder. When set, plain `bello` runs it, and `--task` always overrides. |
| `coder-mod` | GPT-5.6 | Model family for the coder thread. |
| `coder-5.6-variant` | Sol | GPT-5.6 variant for the coder: Sol, Terra, or Luna. |
| `coder-intelligence` | `xhigh` | Coder reasoning effort, limited by the selected variant. |
| `revision-coder` | `off` | Start one fresh coder thread on the first returned completion-review or adversary finding. Later findings reuse that thread. |
| `revision-coder-mod` / `revision-coder-5.6-variant` | GPT-5.6 Sol | Model for the revision thread. Hidden while `revision-coder` is off. |
| `revision-coder-intelligence` | `xhigh` | Revision-coder reasoning effort. Hidden while `revision-coder` is off. |
| `multi-agent` | `off` | Allow the coder to delegate independent work to Codex subagents. When off, subagent tools are disabled for the coder thread. |
| `subagent-max-concurrent` | `4` | Maximum concurrent Codex agent threads in the coder session. Hidden while `multi-agent` is off. |
| `subagent-default-mod` / `subagent-default-5.6-variant` | GPT-5.6 Luna | Default child model. Only models with at least one allowed effort can be selected. |
| `subagent-default-intelligence` | `high` | Default child effort, selected from that model's allowed efforts. |
| `subagent-allowed-*` | Luna: `medium, high, xhigh`; Terra: `medium, high` | Toggle the exact model/effort pairs available to the coder. The active default and final remaining pair cannot be removed. |
| `runtime-mod` | GPT-5.6 | Model family for fresh-context runtime checks, including risky-action judgment and drift detection. |
| `runtime-5.6-variant` | Sol | GPT-5.6 variant for the full runtime supervisor. |
| `runtime-intelligence` | `xhigh` | Full runtime supervisor reasoning effort. |
| `completion-mod` | GPT-5.6 | Model family for the independent completion reviewer. Hidden unless `completion-review` is enabled. |
| `completion-5.6-variant` | Sol | GPT-5.6 variant for completion review. Hidden unless `completion-review` is enabled. |
| `completion-intelligence` | `xhigh` | Completion reviewer reasoning effort. Hidden unless `completion-review` is enabled. |
| `completion-multi-agent` | `off` | Allow the completion reviewer to delegate bounded checks while retaining the final accept-or-return judgment. Hidden unless `completion-review` is enabled. |
| `completion-subagent-*` | Luna `high`, max `4` | Completion-review child concurrency, default profile, and allowed model/effort pairs. Hidden while `completion-multi-agent` is off. |
| `adversary-mod` | GPT-5.6 | Adversarial tester model family. Visible only when the adversary is enabled. |
| `adversary-5.6-variant` | Sol | GPT-5.6 variant for the adversary. Visible only when the adversary is enabled. |
| `adversary-intelligence` | `xhigh` | Adversary reasoning effort. Visible only when the adversary is enabled. |
| `adversary-multi-agent` | `off` | Allow the adversary to delegate independent attack surfaces while retaining the final report judgment. Hidden unless the adversary is enabled. |
| `adversary-subagent-*` | Luna `high`, max `4` | Adversary child concurrency, default profile, and allowed model/effort pairs. Hidden while `adversary-multi-agent` is off. |
| `speed` | `usual` | `fast` uses the Codex Fast service tier for coder, revision-coder, runtime-supervisor, and completion-review turns. Adversary turns are unchanged. |
| `cheap-runtime` | `true` | Let Luna dismiss routine runtime checks before invoking the full runtime supervisor. Human messages, approvals, and mandatory checks bypass triage. |
| `start-over` | `false` | `true` removes prior Bello logs, archived runs, and recovery data, and `false` preserves them. Both start fresh active state and leave project files unchanged. |
| `completion-review` | `false` | `false` runs the `runtime-only` setup. `true` enables the independent completion-review loop and reveals its settings. |
| `adversary` | `false` | Enable the adversarial tester before completion. Requires completion review. |
| `max-reviews` / `max-reviews-before-adversary` | `1` | Completion-return budget. Without an adversary it is shown as `max-reviews`, and with an adversary it limits returns before the first pass. An earlier accept starts the adversary immediately. `0` skips these rounds, and `Unlimited` removes the cap. |
| `max-adversary-runs` | `1` | Maximum adversary passes when the adversary is enabled. `0` disables the adversary. |
| `max-reviews-after-adversary` | `0` | Maximum additional completion-review rounds after each adversary pass. At the limit Bello starts the next pass, or completes after the final one. `0` adds no rounds, and `Unlimited` removes the cap. A candidate adversary finding is still adjudicated once. |
| `clean` | `false` | **Warning:** deletes everything in the folder except the task file and configured protected paths before starting. Only for disposable folders where you want a build from scratch. |
| `protected-path` | absent | Paths the coder must never write to, such as golden tests, fixtures, or production configs. They are also preserved by `clean`. |

## Command reference

```bash
bello                 # run the configured task in the current folder
bello --task TASK.md  # run a specific task file
bello --task TASK.md --plan PLAN.md  # use an existing advisory plan for the initial coder
bello config          # open the interactive config editor
bello doctor          # check Python, git, Codex, auth, app-server support
bello update          # update Bello to the latest version
bello update --check --json  # machine-readable update status
bello --version       # installed version, latest version, update status
```

Run flags (each overrides the saved config for one run):

| Flag | Meaning |
| --- | --- |
| `--task PATH` | Task file to run. |
| `--plan PATH` | Advisory plan for the initial coder; it must be untracked and absent from reachable Git history, and is run-only. |
| `--coder-mod M` | Coder model. |
| `--runtime-mod M` | Runtime supervisor model. |
| `--completion-mod M` | Completion reviewer model. |
| `--adversary-mod M` | Adversarial tester model. |
| `--coder-intelligence V` | Coder reasoning effort. |
| `--runtime-intelligence V` | Runtime supervisor reasoning effort. |
| `--completion-intelligence V` | Completion reviewer reasoning effort. |
| `--adversary-intelligence V` | Adversarial tester reasoning effort. |
| <code>--fast[=true&#124;false]</code> | Codex Fast service tier. |
| <code>--start-over[=true&#124;false]</code> | Fresh `.supervisor/` state. |
| <code>--completion-review[=true&#124;false]</code> | Completion-review loop on or off (`false` runs `runtime-only` and disables the adversary). |
| <code>--adversary[=true&#124;false]</code> | Adversarial tester on or off. |
| `--adversary-runs N` | Adversary pass budget, and `0` disables it. |
| <code>--clean[=true&#124;false]</code> | **Warning:** wipe the folder except the task file, an explicitly supplied plan, and protected paths before starting. |
| `--protected-path PATH` | Protect a path from writes, and repeat the flag for multiple paths. |

Environment variables: `BELLO_SKIP_UPDATE_CHECK=1` skips the startup update
check, `BELLO_PROMPTS_FILE=/path/to/prompts.toml` points Bello at an
alternative prompt file for experiments, and `BELLO_CONFIG_ANIMATIONS=0`
disables motion in the interactive config editor.

## License

Bello is released under the MIT License. See [LICENSE](./LICENSE).

Contributions require signing the project [CLA](./CLA.md). A bot will prompt
you on your first pull request, and you only sign once.
