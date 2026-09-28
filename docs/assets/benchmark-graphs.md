# Benchmark graphics

## Four models, with and without Bello

<picture>
  <source media="(max-width: 600px)" srcset="./readme-model-comparison-mobile.svg">
  <img src="./readme-model-comparison.svg" alt="Four-model comparison: mean score rises from 43.29% to 52.17%." width="100%">
</picture>

## Log distiller

<picture>
  <source media="(max-width: 600px)" srcset="./readme-distiller-mobile.svg">
  <img src="./readme-distiller.svg" alt="Distiller API-equivalent cost versus quality: Astra cost 100 to 77.98, score 62.22% to 60.57%; Luna cost 100 to 85.22, score 56.75% to 55.45%, including runtime. Costs are normalized independently for each model." width="100%">
</picture>

## Smart Execution

<picture>
  <source media="(max-width: 600px)" srcset="./readme-smart-execution-mobile.svg">
  <img src="./readme-smart-execution.svg" alt="Smart Execution: API-equivalent cost falls 30.19% on Sonnet 5 and 31.61% on Astra; solution time falls 50.00% and 15.06%; mean scores decrease by 3.68% and 0.74%." width="100%">
</picture>

## Budget C+A

<picture>
  <source media="(max-width: 600px)" srcset="./programbench-efficient-budget-quality-cost-mobile.svg">
  <img src="./programbench-efficient-budget-quality-cost.svg" alt="Budget C+A versus Raw Sol XHigh: 66.3% lower weekly-limit usage with average quality preserved." width="100%">
</picture>

## Sol Ultra C+A

<picture>
  <source media="(max-width: 600px)" srcset="./programbench-ca-performance-mobile.svg">
  <img src="./programbench-ca-performance.svg" alt="Sol Ultra C+A: 26.41% higher relative mean score; per-task scores and solution times." width="100%">
</picture>

## Runtime-only

<picture>
  <source media="(max-width: 600px)" srcset="./runtime-only-custom-task-results-mobile.svg">
  <img src="./runtime-only-custom-task-results.svg" alt="Runtime-only improves all three task-specific scores: Marl, Slab, and Pinch." width="100%">
</picture>

<details>
<summary>Sources, design references, and regeneration</summary>

Six figures; each has a wide layout and a narrow-screen layout. The README
selects the narrow layout below 600 px. Each SVG has its own light/dark palette,
text alternatives, and no scripts, external fonts, or external images.

Regenerate with Python 3, without installing dependencies:

```sh
python3 scripts/plot_readme_graphs.py
```

## Figures and sources

| Figure | Data | What the comparison means |
| --- | --- | --- |
| `readme-model-comparison` | Reported 24-run table: Luna, Terra, Sol, Astra × Solar, Samtools, rumdl × Raw/Bello | Each point is the unweighted mean of three task scores. 12 Raw + 12 Bello runs, not 24 pairs or three seeds for every task. |
| `readme-distiller` | JSON Schema Astra XHigh 3+3 and Luna Max 6+6 summaries | API-equivalent cost versus score. Raw = 100 for each model. Luna includes runtime. |
| `readme-smart-execution` | Reported Sonnet 5 and Astra RAW/SE runs, three per model and setup | Mean API-equivalent cost, solver time, and score; separate zero-based axes. Relative changes use unrounded means. All three scores per arm are included; Sonnet RAW-01 uses 888/1978 and Astra ON2 uses 1124/1978. |
| `programbench-efficient-budget-quality-cost` | Published four-task Budget comparison, 12+12 runs | Luna-based Budget C+A versus Raw Sol XHigh. Scores are means; usage is summed across three runs per task. Headline usage is summed across all twelve runs per arm. |
| `programbench-ca-performance` | [`programbench_ca_run_info.csv`](../../programbench_ca_run_info.csv) | Sol Ultra Raw versus one completion review + adversary. Not the older 4C+A+2C schedule. Score and solution time stay separate. |
| `runtime-only-custom-task-results` | Published Marl / Slab / Pinch custom-task table | Task-specific scores, not a pooled ProgramBench score. |

Published rounded aggregates are retained; do not recompute them from rounded
per-run rows. Visible value labels use two decimal places; source precision is
retained in the generator and SVG descriptions. `pp` means percentage points;
`% relative` means relative change.
No error bars, fitted curves, or confidence bands are inferred from these data.
All bar lengths start at zero. The overview is a categorical line comparison
whose score axis spans 25–72%, not a continuous model-size curve.

## Visual direction

Only metric labels, legends, axes, categories, and values appear in the figures.
The model overview uses two polylines. The distiller uses a paired cost–quality
scatter plot; connectors pair observations, not fitted trends. Its score axis
spans 50–65%, and its relative-cost axis spans 70–105. Other comparisons use
vertical columns. Raw columns are outlined blue and Bello columns are solid vermilion.
Smart Execution has three panels for cost, solver time, and score; the narrow
layout stacks them vertically. Percentage labels show changes relative to Raw.
The overview also distinguishes its series with hollow/solid markers and
dashed/solid lines. Narrow layouts stack the two-panel comparisons.

References examined for presentation, not for benchmark data:

- [METR time horizons](https://metr.org/time-horizons/): open plotting area and fine grid.
- [Epoch FrontierMath](https://epoch.ai/benchmarks/frontiermath-tier-4): direct labels and clearly separated coloured series.
- [Aider leaderboards](https://aider.chat/docs/leaderboards/): exact, readily comparable values.

No step-lines or interpolation are used: the overview connects only the four
reported categorical means. No animation is necessary to read a result.

</details>
