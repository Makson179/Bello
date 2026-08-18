<h1 align="center">Bello</h1>

Assign the task and walk away. Bello keeps the coder inside a disposable
sandbox while an independent, fresh-context supervisor reviews risky actions,
detects task drift, and manages recovery.

Across three public ProgramBench tasks, Bello outperformed Raw Codex in all comparisons, 
increasing average completion from 44.87% to 61.21%. It is ready to take on your most demanding
tasks.

# Motivation

Long, multi-stage tasks force one model to plan, execute, track requirements, detect errors, and judge completion within a growing context. **Combining all these functions is unreliable.**

Performance degrades as context grows [\[1\]][1] [\[2\]][2] [\[3\]][3] [\[4\]][4], while context compaction may discard critical constraints. Bello moves planning, memory, validation, and acceptance into a separate orchestration layer. It runs a build–review–attack–correct loop and accepts results only after independent confirmation.

## Cognitive foundations

Bello's architecture draws on several foundational areas of cognitive psychology.

In [Allen Newell and Herbert Simon's heuristic search model](https://books.google.com/books?id=h03uAAAAMAAJ), problem solving is viewed as moving from the current state to a goal state through a sequence of operations and subproblems. A person compares the current state with the desired one, chooses an action that reduces the difference, evaluates the result, and restructures the search if the chosen strategy does not work.

[Barry Zimmerman's research on self-regulated learning](https://doi.org/10.1207/S15430421TIP4102_2) describes activity as a recurring cycle of forethought, performance, and self-reflection. The outcome of an evaluation does not end the process; it changes the plan for the next attempt.

[Research on metacognition by John Flavell](https://doi.org/10.1037/0003-066X.34.10.906), [Thomas Nelson, Louis Narens](https://doi.org/10.1016/S0079-7421%2808%2960053-5), and other authors distinguishes between performing a task and managing that performance. One level solves the task; another observes the work in progress, evaluates confidence and evidence, and decides whether to continue, verify, change strategy, or stop.

Similar patterns have also been found in research on writing. [Linda Flower and John Hayes's model](https://doi.org/10.2307/356600) describes writing not as a linear sequence of “plan — draft — edit,” but as a recursive interaction between planning, translating ideas into text, and reviewing. A problem discovered while reading may require more than a local edit: it may call for returning to the goal, structure, or original intent.

The common principle behind this work is that producing a result, monitoring the process, and evaluating it critically should not collapse into a single indistinguishable operation. Reliable reasoning requires cycles, specialized functions, and the ability to revisit earlier decisions.

Bello turns this structure into an executable system.

## How Bello solves tasks

The process begins with an initial implementation. The developer agent analyzes the task, modifies the project, and runs the relevant checks.

An **independent acceptance reviewer** reconstructs the task's requirements and acceptance criteria without relying on the developer's report. It checks:

* whether the required behavior and acceptance criteria are met;
* whether validation evidence covers edge cases and regressions;
* whether trusted behavioral validation passed after the latest relevant source or test change.

If a problem is found, the work returns to the developer. After the fix, a new full review is performed because a local change may affect other parts of the system.

Once the solution has passed several development and review cycles, the **adversary** is launched. Its job is not to confirm the work, but to try to break it. It explores invalid inputs, unexpected action sequences, interactions between features, boundary states, and assumptions that the developer and reviewer may have overlooked.

The adversary works independently of the solution's development history. It evaluates the final artifact, not how convincing the author's explanation is. If it finds a potential defect, the solution is sent back to completion review, which determines whether the observed behavior is a genuine violation of the requirements.

Bello therefore implements the following cycle:

**build a solution → independently review completeness → fix defects → perform adversarial testing → reassess → accept the result.**

## Why this structure is a natural fit for software development

The process mirrors code review, acceptance testing, fuzzing, and red teaming: the developer builds the solution, while independent agents verify requirements and search beyond the happy path.

## Relationship to existing LLM research

Individual parts of this approach have already been tested in language-model research.

[**Self-Refine**](https://arxiv.org/abs/2303.17651) showed that a cycle of generation, critique, and refinement can substantially outperform a single-pass response. [**Reflexion**](https://arxiv.org/abs/2303.11366) demonstrated the value of retaining lessons from previous attempts. [**CRITIC**](https://arxiv.org/abs/2305.11738) connected self-correction with external tools and observable evidence. [Research on self-debugging](https://arxiv.org/abs/2304.05128) confirmed that models can improve code by analyzing execution results. [Work on adversarial testing and verifier-guided search](https://arxiv.org/abs/2604.10449) showed that a separate verifier or attacker can detect seemingly correct solutions that pass conventional checks.

These findings support individual elements of Bello's architecture. Most of this work, however, studies a single mechanism: reflection, correction, verification, debate, test generation, or adversarial search.

Bello combines these mechanisms into a unified system for managing long-running work.

## Results

### Key findings

- Across all three tasks and model–effort settings, Bello achieved the higher
  completion score in **9 of 9 matched configurations**. The overall unweighted
  mean increased from **44.87% to 61.21%**: **+16.33 percentage points**
  (+36.40% relative).
- With GPT-5.6 Sol, Bello achieved the higher completion score in **6 of 6
  matched configurations**. The unweighted mean increased from **48.92% to
  67.04%**: **+18.13 percentage points** (+37.06% relative).
- In the complete GPT-5.6 Sol `ultra` comparison, every task improved by
  **18.17–24.59 points**, and the macro average increased from **53.53% to 74.03%**.
- With GPT-5.5 `xhigh`, Bello scored higher on all three tasks; the macro
  average increased from **36.79% to 49.53%**: **+12.74 percentage points**
  (+34.64% relative).

### Evaluation protocol

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

### Detailed results

| Model / effort | Task | Raw Codex | Bello | Difference (pp) | Codex time | Bello time |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| GPT-5.6 Sol / `ultra` | Solar | 53.13% | **71.30%** | **+18.17** | 00:32:33 | 07:39:17 |
| GPT-5.6 Sol / `ultra` | Samtools | 51.86% | **70.60%** | **+18.74** | 00:36:17 | 19:25:22 |
| GPT-5.6 Sol / `ultra` | Rumdl | 55.60% | **80.19%** | **+24.59** | 01:40:05 | 07:44:12 |
| GPT-5.6 Sol / `ultra` | **Mean / total** | 53.53% | **74.03%** | **+20.50** | **02:48:55** | **34:48:51** |
| GPT-5.6 Sol / `xhigh` | Solar | 46.61% | **66.50%** | **+19.89** | 00:16:58 | 04:26:04 |
| GPT-5.6 Sol / `xhigh` | Samtools | 38.11% | **51.93%** | **+13.82** | 00:28:48 | 05:39:13 |
| GPT-5.6 Sol / `xhigh` | Rumdl | 48.19% | **61.74%** | **+13.55** | 00:31:57 | 03:53:35 |
| GPT-5.6 Sol / `xhigh` | **Mean / total** | 44.30% | **60.06%** | **+15.75** | **01:17:43** | **13:58:52** |
| GPT-5.5 / `xhigh` | Solar | 43.78% | **53.39%** | **+9.61** | 00:16:27 | 01:29:35 |
| GPT-5.5 / `xhigh` | Samtools | 20.28% | **44.21%** | **+23.93** | 00:16:28 | 02:30:01 |
| GPT-5.5 / `xhigh` | Rumdl | 46.30% | **50.99%** | **+4.69** | 00:26:03 | 03:30:01 |
| GPT-5.5 / `xhigh` | **Mean / total** | 36.79% | **49.53%** | **+12.74** | **00:58:58** | **07:29:37** |

*Bold completion values indicate the higher observed score within each matched row. Summary rows report macro means and total runtime.*

### Cross-task completion summary

![Cross-task completion scores for all three model–effort comparisons](./docs/assets/programbench-cross-task-completion.svg)

*Figure 1. Cross-task completion summary on a common 0–100% scale. Panels
(a), (b), and (c) show the matched GPT-5.6 Sol `ultra`, GPT-5.6 Sol `xhigh`,
and GPT-5.5 `xhigh` comparisons. The unweighted macro differences are +20.50,
+15.75, and +12.74 percentage points, respectively.*

### Task-level configuration profiles

The following panels compare all three complete three-task configurations:
GPT-5.5 `xhigh`, GPT-5.6 Sol `xhigh`, and GPT-5.6 Sol `ultra`. Each panel
contains exactly six bars (Raw Codex and Bello for each model–effort setting),
ordered by increasing completion score. Bello precedes Raw Codex when scores
are tied. Ordering is descriptive and does not imply compute equivalence.

![Solar configuration profile](./docs/assets/programbench-solar.svg)

*Figure 2a. Solar completion scores for the six model–effort configurations,
sorted from lowest to highest. The two formerly tied values are shown at their
available precision: Codex GPT-5.6 Sol `ultra` at 53.13% and Bello GPT-5.5
`xhigh` at 53.39%.*

![Samtools configuration profile](./docs/assets/programbench-samtools.svg)

*Figure 2b. Samtools completion scores for the six model–effort
configurations, sorted from lowest to highest.*

![Rumdl configuration profile](./docs/assets/programbench-rumdl.svg)

*Figure 2c. Rumdl completion scores for the six model–effort
configurations, sorted from lowest to highest.*

### A shorter quality–efficiency balance

We also tested a shorter `C+A+C` schedule: it reached 63% completion on
Samtools and 79% on Rumdl. Each completion-review or adversary pass is designed
to find every material defect it can in the solution snapshot it receives, so
each successive pass tends to deliver a smaller quality gain at roughly the
same per-pass cost. The `C+A+C` results, together with an intermediate run in
which the first two completion reviews delivered roughly 80% of the eventual
improvement, support this diminishing-returns pattern. We therefore recommend
`C+A`—one completion review followed by one adversary pass—as the best balance
of quality, time, and cost. We expect it to retain about 60–70% of the full
schedule's quality gain: against the roughly 35% average relative improvement
observed above, that corresponds to an estimated gain of about 20% over Raw
Codex.

Time and cost remain limitations. We estimate that `C+A` takes approximately
2.5× as long as Raw Codex and costs approximately 2.7× as much. The absolute
impact is much smaller than those multipliers suggest: in our observed `ultra`
runs, Raw Codex used about 0.2–0.3% of a weekly usage limit on a substantial
task, while `C+A+C` used at most about 1.2%. In economic terms, the
share of the available budget matters alongside the relative increase: tripling
a negligible expense is less noticeable than a 5% increase in something that
already consumes half the budget. We are actively working to reduce both
runtime and cost without giving up the quality improvement.

## Requirements

- **Codex CLI** installed and authenticated (Bello drives
  `codex app-server`; your Codex account provides the models).
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

Bello checks for updates at startup and offers to install them; run
`bello update` to update explicitly.

## Quick start

```bash
cd your-project
echo "Build a CLI tool that ..." > task.md
bello --task task.md
```

That's it. Bello starts the coder, supervises the run, and writes
`.supervisor/FINAL_REPORT.md` when it finishes: status, changed files,
validations that were run, and remaining risks.

While a run is active you can type into the terminal; your message is routed
to the supervisor, not the coder:

| Control | Action |
| --- | --- |
| `/status` | Show task, generation, active turn, pending approvals, health. |
| `/pause` / `/resume` | Pause and resume the autonomous loop. |
| `/restart` | Request a supervised restart. |
| `/quit` | Write state and exit. |
| any text | Delivered to the supervisor as an instruction or constraint. |

Everything the run does is written to inspectable files under `.supervisor/`
in your project: `PROGRESS.md` (progress log), `DECISIONS.md` (decision log),
`HANDOFF.md` (restart handoff), `events.jsonl` (event log), and
`FINAL_REPORT.md` (final report).

## Run modes

Bello is built for walk-away execution. In both primary modes, the coder works
inside a disposable, network-isolated snapshot rather than directly in your
live project. A fresh-context runtime supervisor evaluates risky or
out-of-sandbox actions, detects task drift, and manages recovery; unsupported
requests and supervisor failures fail closed. Only an accepted, policy-checked
patch is applied to the live project.

The modes differ in what happens after the coder reports validated readiness.

### Everyday (default)

Everyday is for short and medium tasks. It uses runtime supervision and validation gates but skips post-implementation review and adversarial testing.

### Deep Work

Deep Work adds independent acceptance review and adversarial testing. We abbreviate a completion-review round as `C` and an adversary pass as `A`; the default schedule is `C+A`, with no scheduled post-adversary review. Defects found in review return to the coder, while adversarial findings receive independent adjudication before completion.

To enable Deep Work, run `bello config`, set `completion-review` to `true`,
then set `adversary` to `true`. The revealed schedule values default to `1`, `1`,
and `0`: one completion-review return budget before one adversary pass, with no
scheduled post-adversary review rounds. For a single run without rewriting the
saved config:

```bash
bello --task task.md --completion-review=true --adversary=true
```

### Custom

For experiments, configure the coder, runtime supervisor, completion reviewer,
and adversary independently. Each role can use any available GPT-5.6 variant
and its own reasoning effort, and the review and adversary budgets can be
combined freely.

## Configuration

`bello config` edits `.supervisor/config.json` and saves each setting when you press Enter. It shows only options relevant to the selected pipeline. CLI flags override saved settings for one run without modifying the file.

| Setting | Default | What it does |
| --- | --- | --- |
| `task` | absent | Default task file for this folder. When set, plain `bello` runs it; `--task` always overrides. |
| `coder-mod` | GPT-5.6 | Model family for the coder thread. |
| `coder-5.6-variant` | Sol | GPT-5.6 variant for the coder: Sol, Terra, or Luna. |
| `coder-intelligence` | `xhigh` | Coder reasoning effort, limited by the selected variant. |
| `runtime-mod` | GPT-5.6 | Model family for fresh-context runtime checks, including risky-action judgment and drift detection. |
| `runtime-5.6-variant` | Sol | GPT-5.6 variant for the full runtime supervisor. |
| `runtime-intelligence` | `xhigh` | Full runtime supervisor reasoning effort. |
| `completion-mod` | GPT-5.6 | Model family for the independent read-only completion reviewer. Hidden in Everyday mode. |
| `completion-5.6-variant` | Sol | GPT-5.6 variant for completion review. Hidden in Everyday mode. |
| `completion-intelligence` | `xhigh` | Completion reviewer reasoning effort. Hidden in Everyday mode. |
| `adversary-mod` | GPT-5.6 | Adversarial tester model family. Visible only when the adversary is enabled. |
| `adversary-5.6-variant` | Sol | GPT-5.6 variant for the adversary. Visible only when the adversary is enabled. |
| `adversary-intelligence` | `xhigh` | Adversary reasoning effort. Visible only when the adversary is enabled. |
| `speed` | `usual` | `fast` uses the Codex Fast service tier for coder, runtime-supervisor, and completion-review turns. Adversary turns are unchanged. |
| `cheap-runtime` | `true` | Let Luna dismiss routine runtime checks before invoking the full runtime supervisor. Human messages, approvals, and mandatory checks bypass triage. |
| `start-over` | `true` | `true` removes prior Bello logs, archived runs, and recovery data; `false` preserves them. Both start fresh active state and leave project files unchanged. |
| `completion-review` | `false` | `false` is Everyday. `true` enables the independent completion-review loop and reveals its settings. |
| `adversary` | `false` | Enable the adversarial tester before completion. Requires completion review. |
| `max-reviews` / `max-reviews-before-adversary` | `1` | Completion-return budget. Without an adversary it is shown as `max-reviews`; with an adversary it limits returns before the first pass. An earlier accept starts the adversary immediately. `0` skips these rounds; `Unlimited` removes the cap. |
| `max-adversary-runs` | `1` | Maximum adversary passes in Deep Work. `0` disables the adversary. |
| `max-reviews-after-adversary` | `0` | Maximum additional completion-review rounds after each adversary pass. At the limit Bello starts the next pass or completes after the final one. `0` schedules none; `Unlimited` removes the cap. A candidate adversary finding is still adjudicated once. |
| `clean` | `false` | **Warning:** deletes **everything** in the folder except the task file and configured protected paths before starting. Only for disposable folders where you want a from-scratch build. |
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
| <code>--completion-review[=true&#124;false]</code> | Completion-review loop on/off (`false` = Everyday and disables the adversary). |
| <code>--adversary[=true&#124;false]</code> | Adversarial tester on/off. |
| `--adversary-runs N` | Adversary pass budget; `0` disables. |
| <code>--clean[=true&#124;false]</code> | **Warning:** wipe the folder except the task file and protected paths before starting. |
| `--protected-path PATH` | Protect a path from writes; repeat for multiple paths. |

Environment variables: `BELLO_SKIP_UPDATE_CHECK=1` skips the startup update
check; `BELLO_PROMPTS_FILE=/path/to/prompts.toml` points Bello at an
alternative prompt file for experiments; `BELLO_CONFIG_ANIMATIONS=0`
disables motion in the interactive config editor.

## License

Bello is released under the MIT License. See [LICENSE](./LICENSE).

Contributions require signing the project [CLA](./CLA.md); a bot will prompt
you on your first pull request, and you only sign once.

[1]: https://aclanthology.org/2024.tacl-1.9/
[2]: https://arxiv.org/abs/2404.06654
[3]: https://aclanthology.org/2022.acl-long.229/
[4]: https://aclanthology.org/2023.emnlp-main.397/
