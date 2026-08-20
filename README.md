<h1 align="center">Bello</h1>

<p align="center">
  <strong>An autonomous coding loop with independent verification.</strong><br>
  You write the task in a file and leave. Bello keeps the coder inside a disposable sandbox while a separate supervisor, running on a fresh context, reviews risky actions, catches drift, and handles recovery. On ProgramBench, averaged over nine matched runs with GPT-5.5 and GPT-5.6 Sol, the highest-effort setup raised completion by 36.4% relative to Raw Codex. <br>
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
- [Motivation](#motivation)
- [How Bello solves tasks](#how-bello-solves-tasks)
- [Relationship to existing LLM research](#relationship-to-existing-llm-research)
- [Choose your supervision depth](#choose-your-supervision-depth)
- [Results](#results)
- [Requirements](#requirements)
- [Install](#install)
- [Quick start](#quick-start)
- [Configuration](#configuration)
- [Command reference](#command-reference)
- [License](#license)

---

## TL;DR

Bello runs the Codex coder inside a disposable sandbox and puts separate roles
around it: a supervisor that watches the live run, an independent completion
reviewer, and an adversary that tries to break the result. You choose how many
of those roles a task gets. In the names below, `C` is one completion review and
`A` is one adversarial pass.

| Effort | What it does | Measured result | When to use |
| --- | --- | --- | --- |
| `runtime-only` | A supervisor with a fresh context watches the live run, blocks dangerous actions, and pulls the coder back when it drifts. | Time and cost match Raw Codex, and scores run about 9% higher on messy tasks with many requirements. | The everyday default, on any task. |
| `C+A` | Adds one independent completion review and one adversarial pass on top of runtime supervision. | ProgramBench macro completion 53.53% to 67.67%. Runs about 2.5 times longer and costs about 2 times more than Raw Codex, and one benchmark task still used only about 1% of a weekly Codex limit. | A hard task you start in the evening and collect in the morning. |
| `4C+A+2C` | Allows up to four review rounds before the attack and two after it. | The higher score in all nine matched runs, and 36.4% higher completion than Raw Codex on average. Significantly more expensive than Raw Codex. | The hardest tasks, where quality is the priority and cost does not matter. |

Runtime supervision stays active at every effort level. The three rows are
configuration recipes rather than built-in modes, and
[Choose your supervision depth](#choose-your-supervision-depth) explains the
fields and how to build your own.

---

# Motivation

Modern language models can write code, analyze documents, and solve hard
problems, but a model still produces its answer one step at a time. On a long,
multi-stage task, the same model has to hold the requirements, plan the work,
carry it out, judge its own progress, notice its own mistakes, and decide when
the result can be called finished.

Doing all of that inside one model is unreliable. As the context grows, model
quality drops quickly and hallucination becomes more likely
[[1]](https://aclanthology.org/2024.tacl-1.9/)
[[2]](https://arxiv.org/abs/2404.06654)
[[3]](https://aclanthology.org/2022.acl-long.229/)
[[4]](https://aclanthology.org/2023.emnlp-main.397/). Compressing the history
reduces the context problem, but compression can drop a rule, a decision, or a
prohibition that still applies. A confident report from the model is also not
evidence that the task was actually completed.

Bello moves the orchestration, the state, and the control of complex work
outside the language model.

The coder still plans its own work and derives the requirements from the task,
because a language model is good at exactly that. What Bello keeps outside the
model is everything around it: which role runs when, what state survives a
restart, what the coder is allowed to do, and who decides that the work is
finished. With completion review and the adversary enabled, Bello runs a
repeatable loop in which a solution is written, reviewed, attacked, corrected,
and accepted only after an independent check confirms it.

## How Bello solves tasks

The run starts by building the first complete solution. The coder in the
isolated sandbox reads the task, modifies the project, runs checks, and produces
a working prototype. While the coder works, a runtime supervisor with a fresh
context watches the execution, blocks risky actions before they happen, and
steers the coder back on track when it detects drift, repeated mistakes, or
unsafe behavior. The supervisor can also deny an action or restart a failing
generation, and a restart keeps the coder's current workspace, so the run stays
autonomous and still under control.

When completion review is enabled and a review opportunity remains, the result
then goes to an independent **completion review**. The reviewer does not continue
development, and it does not accept the coder's report as evidence. It
reconstructs the mandatory requirements of the task on its own and checks:

* whether the required behavior has been implemented;
* whether the checks support the claimed result;
* whether any modes or edge cases remain untested;
* whether any regressions have been introduced;
* whether fresh validation was performed after the latest substantial changes.

When the reviewer finds a problem, the work goes back to the coder. After the
fix, the reviewer runs a full review again while the budget still holds a review
opportunity, because a local change can affect other parts of the system. Once
the budget is spent, Bello moves on to the adversary, or finishes the run when
the adversary is off.

Bello starts the **adversary** when the reviewer accepts the result, or when the
review budget before the attack runs out. With `max-reviews-before-adversary`
set to `0` the adversary runs on the first solution, without any review before
it. The adversary tries to break the result.
It explores invalid inputs, unexpected action sequences, interactions between
features, boundary states, and assumptions that the coder and the reviewer may
have overlooked.

The adversary works without the development history of the solution. It judges
the final artifact rather than the author's explanation. Its report goes to a
separate report controller, which checks every finding, keeps the confirmed ones,
rejects the incorrect ones, and downgrades the doubtful ones to observations. The
coder then receives the surviving findings together with all observations.

If a run ends unexpectedly after the coder has started working, for example
because of a usage limit, a provider error, or an interrupted process, Bello
preserves the coder's current workspace under `.supervisor/`, including changes
that were never validated. To keep that recovery state available on the next
run, leave Start over disabled (`start-over: false`, the default). If a security
policy interrupted the run, restart it with `--start-over=false`. Enabling Start
over discards previous recovery data.

With every stage enabled, Bello therefore implements the following cycle:

**build a solution → independently review completeness → fix defects → perform adversarial testing → reassess → accept the result.**

## Relationship to existing LLM research

Bello separates iterative repair from acceptance.
[Is Self-Repair a Silver Bullet for Code Generation?](https://arxiv.org/abs/2306.09896)
found that cost-adjusted self-repair gains were often modest, variable, or
absent, and that they increased substantially when feedback came from a stronger
model or a human. [CRITIC](https://arxiv.org/abs/2305.11738) provides the
complementary result that correction is more reliable when it is grounded in
observable feedback from external tools. Bello therefore lets the coder execute
tests and repair the artifact, but does not let the authoring trajectory certify
completion. Acceptance is decided by a fresh reviewer that does not modify the
artifact and does not treat the coder's report as evidence. The reviewer reads
the specification, the artifact, and the diff, and it obtains its own behavioral
evidence by selectively rerunning checks against the result. The diff makes that
evidence harder to stage, because weakened assertions, skipped cases,
substituted mocks, and deleted tests all appear as changes even when the suite
reports green. [StackEval](https://arxiv.org/abs/2412.05288) found that reference
answers consistently improved LLM code-judging accuracy, and it detected no
statistically significant self-preference when such references were supplied.
The finding supports review anchored in evidence, although StackEval's one-shot
setting does not establish that a fresh reviewer is an independent correctness
oracle. In Bello, the use of a fresh context separates the acceptance decision
from the coder's trajectory, and validation remains necessary.

The adversarial stage addresses weaknesses in both fixed and model-generated
tests. [EvalPlus](https://arxiv.org/abs/2305.01210) showed that the original
HumanEval suites accepted substantial amounts of functionally incorrect code.
[Revisit Self-Debugging with Self-Generated Tests for Code Generation](https://arxiv.org/abs/2501.12793)
found that self-generated tests can produce biased and misleading repair
signals. Taken together, the studies above and the 2026 preprint
[AdverMCTS](https://arxiv.org/abs/2604.10449) provide the closest evidence for
Bello's attacker role. In AdverMCTS, targeted corner cases reduced
pseudo-correctness caused by sparse static tests, in a setting of programming
problems. Bello accordingly separates implementation, counterexample generation,
and acceptance. The adversary searches beyond the existing suite, but its tests
are candidate evidence rather than ground truth. A separate report controller
checks each finding and drops the ones it cannot confirm before the coder sees
the report, acceptance stays with the completion reviewer whenever review rounds
are scheduled after the attack, and relevant edits invalidate earlier acceptance
evidence.

The [AgentCoder](https://arxiv.org/abs/2312.13010) preprint is the closest prior
architecture. It separates a programmer, an implementation-independent test
designer, and a test executor, and its ablations support separating test
construction from code generation. Its evaluation is limited to function-level
synthesis, and it treats a task as complete when the generated tests pass. It
therefore supports Bello's role separation without covering long-running runtime
supervision, a separate completion gate, a separate handler for adversary
findings, or restart state. The additional controls in Bello target failures
identified by [MAST](https://arxiv.org/abs/2503.13657) across more than 1,600
multi-agent traces, including role violations, history loss, task derailment, premature
termination, and absent or incorrect verification. Bello maps them to fixed role
contracts, durable handoffs, live drift detection, explicit stage transitions,
and a separate final acceptance decision. Multi-agent specialization is prior
art, and Bello's architectural claim concerns the governance and evidence
requirements imposed around the roles.

Finally, [CaMeL](https://arxiv.org/abs/2503.18813) demonstrates a
prompt-injection defense in which trusted control flow and security policy are
enforced by a protective system layer rather than delegated to model compliance.
Bello applies the same principle through isolated execution, mediated actions,
live runtime supervision, and fail-closed approvals, without claiming CaMeL's
capability model or formal guarantees.

## Choose your supervision depth

Bello can be used as a light safety layer or as a full quality pipeline. In the
effort levels below, `C` is an independent **completion review** and `A` is an
**adversarial pass**. Runtime supervision stays active at every effort level, and
the [TL;DR table](#tldr) summarizes the three of them.

### `runtime-only`, for everyday work

The coder works as usual while a supervisor with a clean context watches the
live trajectory. The supervisor stops abrupt, irreversible actions, such as
dropping a database or cancelling a paid subscription, and it redirects a coder
that has drifted away from the task. On average it matches Raw Codex on both
time and cost, and on individual tasks it is sometimes faster and cheaper,
because a coder that is kept on track does less useless work.

Use `runtime-only` as the default for any task. It removes most of the risk that
the coder starts hallucinating and doing damage, and the quality gain is largest
when the task is written the way people normally write tasks at work: long,
messy, and full of requirements added in passing. On our three custom tasks of
that kind, `runtime-only` scored about 9% higher than Raw Codex, because it
catches drift and hallucination early. The ProgramBench tasks are short, so they
understate the effect, and there the mean gain was about 2%.

### `C+A`, for heavy overnight work

This effort level adds up to one independent completion-review round followed by an
adversarial attempt to break the result, and it keeps every `runtime-only`
protection. On ProgramBench it raised macro completion from 53.53% to 67.67%,
which is **69% of the improvement** delivered by the full `4C+A+2C` setup in
our shorter-run comparison. The gain costs time and money. The three-task run
took about 2.5 times longer than Raw Codex, and it cost roughly 1.8 to 2.3 times
more.

The absolute numbers stay small. One ProgramBench task consumed about 0.3% to
0.4% of a weekly Codex limit under Raw Codex, and up to 1.2% under `C+A`, so a
weekly quota still covers dozens of runs.

Use `C+A` when you want to hand over a hard task at the end of the day and need
serious quality with a real review behind it. The run finishes overnight, and the
result is clearly better than what Raw Codex produces on the same task.

### `4C+A+2C`, for maximum quality

This effort level allows up to four completion-review rounds to refine the
implementation before the adversary probes its assumptions, and up to two further
rounds to resolve what the attack uncovers. We built it to see how high Bello can
score on a benchmark with every stage enabled, and the measured completion was
the highest of the three effort levels. Bello scored higher in all nine matched
runs. Averaged over them it improved completion by **36.4% over Raw Codex**, and
in the GPT-5.6 Sol `ultra` comparison by 38.30%.

This effort level is significantly more expensive than Raw Codex and takes much
longer, so it is worth choosing deliberately. It fits a genuinely awkward task
with many cases and nuances, where quality is the priority and cost is not a
constraint. Plan for a long run, because in our `ultra` runs a single task took
between 7 hours 39 minutes and 19 hours 25 minutes.

Configure these effort levels with `bello config`. For `runtime-only`, set
`completion-review` and `adversary` to `false`. For `C+A`, enable both and set
`max-reviews-before-adversary`, `max-adversary-runs`, and
`max-reviews-after-adversary` to `1`, `1`, and `0`. For `4C+A+2C`, use `4`, `1`,
and `2`. The fields, defaults, and one-run CLI overrides are documented in the
[Configuration section](#configuration).

### Custom effort levels

Bello has no built-in list of modes to pick from. The three effort levels above are
configuration recipes, and the budget fields are independent numbers, so you can
choose whatever effort level your task needs: a single `C` with the adversary off,
`2C+A`, `C+A+C`, `4C+2A`, and so on. Set `max-reviews-before-adversary` for the
review rounds before the first attack, `max-adversary-runs` for the number of
adversary passes, and `max-reviews-after-adversary` for the review rounds that
follow each pass. Both review budgets also accept `Unlimited`, which removes the
cap and lets the loop keep going until the reviewer accepts the result.

The numbers are upper limits rather than a fixed sequence. A run can end before
it uses them, because the reviewer can accept early and the run completes
once nothing further is scheduled, so a name like `4C+2A` describes the most the
run may do rather than what it will do. The adversary also requires
`completion-review` to be enabled, and setting `max-reviews-before-adversary` to
`0` is allowed, which sends the first solution straight to the attack.

## Results

### 1. `runtime-only`, low-cost protection

On the three ProgramBench tasks, Solar, Samtools, and Rumdl, `runtime-only`
improved average completion by approximately **2%** over Raw Codex. The larger
benefit is risk control. A fresh supervisor can catch a dangerous action or a bad
trajectory before it becomes an unrecoverable final result, without the cost of
scheduled completion-review and adversary rounds.

We also tested `runtime-only` on
[three custom tasks](https://drive.google.com/drive/u/1/folders/1eLut349Wu_uxw59H6u87cuWNRqYb3x7x)
designed to resemble ordinary work rather than polished benchmark prompts. Their
briefs are deliberately incomplete, awkward, and uneven, the way a task is often
described by a normal colleague at work.

| Task | Raw Codex score | `runtime-only` score | Difference | Raw Codex time | `runtime-only` time |
| --- | ---: | ---: | ---: | ---: | ---: |
| Marl (weighted spec completeness) | 32.91% | **37.91%** | **+5.00 pp** | 00:58:49 | 00:46:09 |
| Slab | 81.08% | **85.69%** | **+4.61 pp** | 00:57:26 | 01:04:11 |
| Pinch | 89.25% | **98.00%** | **+8.75 pp** | 00:40:09 | 00:43:34 |

![Runtime-only results on custom workplace-style tasks](./docs/assets/runtime-only-custom-task-results.svg)

*Figure R1. Comparable 0 to 100 evaluator scores for Marl, Slab, and Pinch. The
scores are separate task-specific measures, not components of a pooled
benchmark.*

The [linked](https://drive.google.com/drive/u/1/folders/1eLut349Wu_uxw59H6u87cuWNRqYb3x7x)
folder contains the complete task briefs, tests, evaluator outputs, and result
artifacts.

### 2. `C+A`, a shorter balance of quality and cost

With GPT-5.6 Sol at `ultra`, C+A raised the unweighted macro completion score
from **53.53% to 67.67%**: **+14.14 percentage points** (**+26.41% relative**).
The three-task runtime was **07:08:06**, compared with **02:48:55** for Raw
Codex. The corresponding rows are available in the
[C+A run-level data](./programbench_ca_run_info.csv).
The corresponding Bello solutions are available in the
[C+A solution artifacts folder](https://drive.google.com/drive/u/1/folders/1oWR5v3fziEZj1PkQ8xDyq5JBRCUPf5gV).

| Task | Raw Codex completion | C+A completion | Difference (pp) | Relative change | Raw Codex time | C+A time |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Solar | 53.13% | **59.00%** | **+5.87** | +11.05% | 00:32:33 | 02:17:45 |
| Samtools | 51.86% | **63.00%** | **+11.14** | +21.48% | 00:36:17 | 02:14:40 |
| Rumdl | 55.60% | **81.00%** | **+25.40** | +45.68% | 01:40:05 | 02:35:41 |
| **Macro mean / total time** | 53.53% | **67.67%** | **+14.14** | **+26.41%** | **02:48:55** | **07:08:06** |

![C+A completion and runtime compared with Raw Codex](./docs/assets/programbench-ca-performance.svg)

*Figure C1. ProgramBench completion and runtime for the three matched GPT-5.6
Sol `ultra` task configurations.*

### 3. `4C+A+2C`, maximum effort

#### Key findings

- Across all three tasks and all model and effort settings, Bello achieved the
  higher completion score in **9 of 9 matched configurations**. The overall
  unweighted mean increased from **44.87% to 61.21%**: **+16.33 percentage
  points** (+36.40% relative).
- With GPT-5.6 Sol, Bello achieved the higher completion score in **6 of 6
  matched configurations**. The unweighted mean increased from **48.92% to
  67.04%**: **+18.13 percentage points** (+37.06% relative).
- In the complete GPT-5.6 Sol `ultra` comparison, every task improved by
  **18.17 to 24.59 points**, and the macro average increased from **53.53% to
  74.03%**.
- With GPT-5.5 `xhigh`, Bello scored higher on all three tasks, and the macro
  average increased from **36.79% to 49.53%**: **+12.74 percentage points**
  (+34.64% relative).

#### Evaluation protocol

We evaluated Bello on three ProgramBench tasks: **Solar**, **Samtools**, and
**Rumdl**. Raw Codex and Bello were observed on every task with GPT-5.6 Sol in
both `ultra` and `xhigh` modes and with GPT-5.5 in `xhigh` mode. We report the
completion score recorded in the `completion_pct` field and time from the
`runtime` field of the [run-level data](./programbench_run_info.csv).
Completion scores are rounded to the nearest hundredth of a percentage point.
Runtime was not held constant, so the comparison is not compute matched.
The final solution patches for all nine reported Bello runs, together with
SHA-256 checksums, are available in the
[public evaluation artifacts folder](https://drive.google.com/drive/folders/1MSyxidKXeQz7DA0gKn6KJtcWmefFu2-D?usp=share_link).

#### GPT-5.6 Sol

##### `ultra`

| Task | Raw Codex completion | Bello completion | Difference (pp) | Relative change | Raw Codex time | Bello time |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Solar | 53.13% | **71.30%** | **+18.17** | +34.20% | 00:32:33 | 07:39:17 |
| Samtools | 51.86% | **70.60%** | **+18.74** | +36.14% | 00:36:17 | 19:25:22 |
| Rumdl | 55.60% | **80.19%** | **+24.59** | +44.23% | 01:40:05 | 07:44:12 |
| **Macro mean / total time** | 53.53% | **74.03%** | **+20.50** | **+38.30%** | **02:48:55** | **34:48:51** |

*Bold completion values indicate the higher observed score within each matched
row.*

Across the three matched `ultra` runs, Bello increased completion by 18.17 to
24.59 percentage points on every task. The unweighted macro average rose from
53.53% to 74.03%, a gain of 20.50 points (38.30% relative).

![GPT-5.6 Sol ultra completion-score differences](./docs/assets/programbench-5-6-ultra-matched-differences.svg)

*Figure 1a. Bello-minus-Raw completion differences for the three GPT-5.6 Sol
`ultra` configurations. Every point lies to the right of zero, and the diamond
shows the unweighted mean difference (+20.50 points). Uncertainty intervals are
not shown because each configuration has one observation.*

##### `xhigh`

| Task | Raw Codex completion | Bello completion | Difference (pp) | Relative change | Raw Codex time | Bello time |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Solar | 46.61% | **66.50%** | **+19.89** | +42.67% | 00:16:58 | 04:26:04 |
| Samtools | 38.11% | **51.93%** | **+13.82** | +36.26% | 00:28:48 | 05:39:13 |
| Rumdl | 48.19% | **61.74%** | **+13.55** | +28.12% | 00:31:57 | 03:53:35 |
| **Macro mean / total time** | 44.30% | **60.06%** | **+15.75** | **+35.56%** | **01:17:43** | **13:58:52** |

*Bold completion values indicate the higher observed score within each matched
row.*

All three `xhigh` tasks improved. The gains ranged from 13.55 to 19.89
percentage points, and the unweighted macro average increased from 44.30% to
60.06% (+15.75 points, +35.56% relative).

![GPT-5.6 Sol xhigh completion-score differences](./docs/assets/programbench-5-6-xhigh-matched-differences.svg)

*Figure 1b. Bello-minus-Raw completion differences for the three GPT-5.6 Sol
`xhigh` configurations. Every point lies to the right of zero, and the diamond
shows the unweighted mean difference (+15.75 points). Uncertainty intervals are
not shown because each configuration has one observation.*

#### GPT-5.5

##### `xhigh`

| Task | Raw Codex completion | Bello completion | Difference (pp) | Relative change | Raw Codex time | Bello time |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Solar | 43.78% | **53.39%** | **+9.61** | +21.95% | 00:16:27 | 01:29:35 |
| Samtools | 20.28% | **44.21%** | **+23.93** | +118.00% | 00:16:28 | 02:30:01 |
| Rumdl | 46.30% | **50.99%** | **+4.69** | +10.13% | 00:26:03 | 03:30:01 |
| **Macro mean / total time** | 36.79% | **49.53%** | **+12.74** | **+34.64%** | **00:58:58** | **07:29:37** |

*Bold completion values indicate the higher observed score within each matched
row.*

Bello's score was higher on all three tasks. The task-level differences ranged
from 4.69 to 23.93 percentage points, and the unweighted macro average increased
from 36.79% to 49.53%, a gain of 12.74 points (34.64% relative).

#### Cross-task completion summary

![Cross-task completion scores for all three model and effort comparisons](./docs/assets/programbench-cross-task-completion.svg)

*Figure 2. Cross-task completion summary on a common 0% to 100% scale. Panels
(a), (b), and (c) show the matched GPT-5.6 Sol `ultra`, GPT-5.6 Sol `xhigh`,
and GPT-5.5 `xhigh` comparisons. The unweighted macro differences are +20.50,
+15.75, and +12.74 percentage points, respectively.*

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

## Requirements

- **Codex CLI** installed and authenticated. Bello drives `codex app-server`,
  and your Codex account provides the models.
- **Python 3.11+** and **git**.
- macOS or Linux.

Verify your environment at any time with `bello doctor`.

## Install

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

```bash
cd your-project
echo "Build a CLI tool that ..." > task.md
bello --task task.md
```

Bello starts the coder, supervises the run, and writes
`.supervisor/FINAL_REPORT.md` when it finishes. The report lists the status, the
changed files, the validations that were run, and the remaining risks.

While a run is active you can type into the terminal, and your message is routed
to the supervisor rather than the coder:

| Control | Action |
| --- | --- |
| `/status` | Show task, generation, active turn, pending approvals, health. |
| `/pause` / `/resume` | Pause and resume the autonomous loop. |
| `/restart` | Request a supervised restart. |
| `/quit` | Write state and exit. |
| any text | Delivered to the supervisor as an instruction or constraint. |

Everything the run does is written to inspectable files under `.supervisor/`
in your project: `PROGRESS.md` (what has happened), `DECISIONS.md` (standing
decisions), `HANDOFF.md` (restart context), `events.jsonl` (full event
stream), and `FINAL_REPORT.md` (the result).

## Configuration

Open the interactive editor from your project folder:

```bash
bello config
```

It creates and edits `.supervisor/config.json`. Every value is saved as you
press Enter, and future runs in this folder use these settings automatically.

For a new project the editor starts with `completion-review` and `adversary`
turned off, which is the `runtime-only` setup, and it only shows settings
that can affect the selected pipeline. Turning on `completion-review` reveals
the completion reviewer and review budget. Turning on `adversary` then reveals
the adversary model and the complete `C+A` setup.

For each visible role, select GPT-5.6 and then choose Sol, Terra, or Luna in
the variant row. Sol and Terra support reasoning effort from `low` through
`ultra`, and Luna supports `low` through `max`. Active primary roles default to
GPT-5.6 Sol at `xhigh`, and cheap runtime triage uses Luna.

CLI flags override their corresponding saved settings for one run and never
rewrite the project config. Settings without a CLI flag, including cheap
runtime and review budgets, are changed through `bello config`.

| Setting | Default | What it does |
| --- | --- | --- |
| `task` | absent | Default task file for this folder. When set, plain `bello` runs it, and `--task` always overrides. |
| `coder-mod` | GPT-5.6 | Model family for the coder thread. |
| `coder-5.6-variant` | Sol | GPT-5.6 variant for the coder: Sol, Terra, or Luna. |
| `coder-intelligence` | `xhigh` | Coder reasoning effort, limited by the selected variant. |
| `runtime-mod` | GPT-5.6 | Model family for fresh-context runtime checks, including risky-action judgment and drift detection. |
| `runtime-5.6-variant` | Sol | GPT-5.6 variant for the full runtime supervisor. |
| `runtime-intelligence` | `xhigh` | Full runtime supervisor reasoning effort. |
| `completion-mod` | GPT-5.6 | Model family for the independent completion reviewer. Hidden unless `completion-review` is enabled. |
| `completion-5.6-variant` | Sol | GPT-5.6 variant for completion review. Hidden unless `completion-review` is enabled. |
| `completion-intelligence` | `xhigh` | Completion reviewer reasoning effort. Hidden unless `completion-review` is enabled. |
| `adversary-mod` | GPT-5.6 | Adversarial tester model family. Visible only when the adversary is enabled. |
| `adversary-5.6-variant` | Sol | GPT-5.6 variant for the adversary. Visible only when the adversary is enabled. |
| `adversary-intelligence` | `xhigh` | Adversary reasoning effort. Visible only when the adversary is enabled. |
| `speed` | `usual` | `fast` uses the Codex Fast service tier for coder, runtime-supervisor, and completion-review turns. Adversary turns are unchanged. |
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
| <code>--clean[=true&#124;false]</code> | **Warning:** wipe the folder except the task file and protected paths before starting. |
| `--protected-path PATH` | Protect a path from writes, and repeat the flag for multiple paths. |

Environment variables: `BELLO_SKIP_UPDATE_CHECK=1` skips the startup update
check, `BELLO_PROMPTS_FILE=/path/to/prompts.toml` points Bello at an
alternative prompt file for experiments, and `BELLO_CONFIG_ANIMATIONS=0`
disables motion in the interactive config editor.

## License

Bello is released under the MIT License. See [LICENSE](./LICENSE).

Contributions require signing the project [CLA](./CLA.md). A bot will prompt
you on your first pull request, and you only sign once.
