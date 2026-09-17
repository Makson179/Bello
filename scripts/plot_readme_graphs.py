#!/usr/bin/env python3
"""Render the README benchmark plates, using only the Python standard library.

Run from any directory: python3 scripts/plot_readme_graphs.py
SVGs have light/dark palettes and separate narrow-screen layouts. No smoothing,
inferred uncertainty or generated data. See docs/assets/benchmark-graphs.md.
"""

from __future__ import annotations

import csv
from html import escape
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "assets"

MODELS = [
    ("Luna", [42.67, 26.18, 29.41], [44.50, 33.12, 61.35]),
    ("Terra", [45.15, 27.51, 22.58], [46.81, 36.42, 40.49]),
    ("Sol", [49.44, 45.47, 52.02], [55.34, 49.00, 61.98]),
    ("Astra", [55.76, 56.84, 66.50], [63.55, 65.47, 68.03]),
]
BUDGET = [
    ("Revive", 40.990, 45.530, 3.2757, 1.0555),
    ("JSON Schema", 56.821, 54.673, 3.0069, .8138),
    ("Lightning CSS", 60.750, 60.302, 6.0281, 2.2569),
    ("Miller", 33.839, 34.684, 3.3190, 1.1428),
]
RUNTIME = [
    ("Marl", 32.91, 37.91, "58:49", "46:09"),
    ("Slab", 81.08, 85.69, "57:26", "1:04:11"),
    ("Pinch", 89.25, 98.00, "40:09", "43:34"),
]

CSS = """
svg { --bg:#fff; --ink:#1e2934; --muted:#596672; --grid:#e2e7ec;
  --raw:#4b709b; --bello:#c4472c; --soft:#f4f6f8; --connector:#c3cbd3; }
@media (prefers-color-scheme:dark) {
  svg { --bg:#0d1117; --ink:#edf2f7; --muted:#a6b2bf; --grid:#2b3542;
    --raw:#94b8e4; --bello:#ff997e; --soft:#171f29; --connector:#526275; }
}
.paper { fill:var(--bg); }
text { font-family:Arial,Helvetica,sans-serif; fill:var(--ink);
  font-variant-numeric:tabular-nums; }
.muted { fill:var(--muted); }
.raw { fill:var(--raw); }
.bello { fill:var(--bello); }
.overline { fill:var(--muted); font-family:ui-monospace,SFMono-Regular,Consolas,monospace;
  letter-spacing:1.6px; }
.halo { paint-order:stroke; stroke:var(--bg); stroke-width:6; stroke-linejoin:round; }
.grid { stroke:var(--grid); stroke-width:1; }
.connector { stroke:var(--connector); stroke-width:3; }
.raw-line { stroke:var(--raw); fill:none; stroke-width:3; stroke-dasharray:8 5; }
.bello-line { stroke:var(--bello); fill:none; stroke-width:4; }
.raw-point { fill:var(--bg); stroke:var(--raw); stroke-width:2.8; }
.bello-point { fill:var(--bello); stroke:var(--bg); stroke-width:1.5; }
.raw-bar { fill:var(--raw); fill-opacity:.16; stroke:var(--raw); stroke-width:1.5; }
.bello-bar { fill:var(--bello); }
"""


class Plate:
    def __init__(self, name, height, title, desc, mobile=False):
        self.name = name + ("-mobile" if mobile else "")
        self.w = 480 if mobile else 1080
        self.h = height
        self.mobile = mobile
        self.margin = 24 if mobile else 40
        self.parts = [
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{self.w}" height="{height}" '
            f'viewBox="0 0 {self.w} {height}" role="img" '
            f'aria-labelledby="{self.name}-title {self.name}-desc">',
            f'<title id="{self.name}-title">{escape(title)}</title>',
            f'<desc id="{self.name}-desc">{escape(desc)}</desc>',
            f'<style>{CSS}</style>',
            f'<rect class="paper" width="{self.w}" height="{height}"/>',
        ]

    def text(self, x, y, text, size=20, cls="", weight=400, anchor="start"):
        self.parts.append(f'<text x="{x:.2f}" y="{y:.2f}" font-size="{size}" '
                          f'font-weight="{weight}" class="{cls}" text-anchor="{anchor}">'
                          f'{escape(str(text))}</text>')

    def line(self, x1, y1, x2, y2, cls="grid", extra=""):
        self.parts.append(f'<line x1="{x1:.2f}" y1="{y1:.2f}" x2="{x2:.2f}" '
                          f'y2="{y2:.2f}" class="{cls}" {extra}/>')

    def rect(self, x, y, w, h, cls, extra=""):
        self.parts.append(f'<rect x="{x:.2f}" y="{y:.2f}" width="{w:.2f}" '
                          f'height="{h:.2f}" class="{cls}" {extra}/>')

    def point(self, x, y, series, r=6):
        if series == "raw":
            self.parts.append(f'<circle cx="{x:.2f}" cy="{y:.2f}" r="{r}" class="raw-point"/>')
        else:
            self.parts.append(f'<path d="M{x:.2f} {y-r-1:.2f} l{r+1} {r+1} '
                              f'l{-r-1} {r+1} l{-r-1} {-r-1} Z" class="bello-point"/>')

    def polyline(self, points, cls):
        p = " ".join(f"{x:.2f},{y:.2f}" for x, y in points)
        self.parts.append(f'<polyline points="{p}" class="{cls}"/>')

    def header(self, number, title, subtitle):
        x = self.margin
        self.text(x, 31, f"{number:02d} / BELLO BENCHMARKS", 13 if self.mobile else 15, "overline")
        lines = title if isinstance(title, list) else [title]
        for i, t in enumerate(lines):
            self.text(x, 72 + i * 34, t, 29 if self.mobile else 35, weight=700)
        y = 103 + 34 * (len(lines)-1)
        subs = subtitle if isinstance(subtitle, list) else [subtitle]
        for i, t in enumerate(subs):
            self.text(x, y + i * 23, t, 16 if self.mobile else 19, "muted")

    def legend(self, x, y, raw="Raw", bello="Bello", gap=150, size=18):
        self.point(x + 6, y-5, "raw", 5)
        self.text(x+21, y, raw, size, "raw", 600)
        self.point(x+gap+6, y-5, "bello", 5)
        self.text(x+gap+21, y, bello, size, "bello", 600)

    def save(self):
        self.parts.append("</svg>")
        (OUT / f"{self.name}.svg").write_text("\n".join(self.parts) + "\n", encoding="utf-8")


def mean(xs):
    return sum(xs) / len(xs)


def model_comparison(mobile):
    values = [(n, mean(r), mean(b)) for n, r, b in MODELS]
    desc = "; ".join(f"{n}: Raw {r:.2f}%, Bello {b:.2f}%" for n, r, b in values)
    p = Plate("readme-model-comparison", 625 if mobile else 650,
              "Four models, with and without Bello", desc +
              ". Each point is the mean reported score on Solar, Samtools and rumdl. "
              "24 runs total: 12 Raw and 12 Bello. Score axis spans 25–72%.", mobile)
    p.header(1, ["Four models.", "Two trajectories."] if mobile else "Four models. Two trajectories.",
             "Mean ProgramBench score · higher is better")
    p.legend(25 if mobile else 760, 166 if mobile else 100, gap=100 if mobile else 125)
    xlist = [65, 185, 305, 425] if mobile else [115, 355, 595, 835]
    top, bottom = (203, 430) if mobile else (153, 465)
    left, right = (48, 451) if mobile else (80, 1000)
    ypos = lambda v: bottom - (v-25) / 47 * (bottom-top)
    for v in [30, 40, 50, 60, 70]:
        y = ypos(v)
        p.line(left, y, right, y)
        p.text(left-10, y+5, v, 14 if mobile else 17, "muted", anchor="end")
    for series, idx in [("raw", 1), ("bello", 2)]:
        p.polyline([(x, ypos(row[idx])) for x, row in zip(xlist, values)], series+"-line")
        for x, row in zip(xlist, values):
            v = row[idx]
            p.point(x, ypos(v), series, 6 if mobile else 7)
            p.text(x, ypos(v) + (28 if series == "raw" else -18), f"{v:.2f}",
                   19 if mobile else 26, series+" halo", 700, "middle")
    if not mobile:
        p.text(887, ypos(values[-1][2])+7, "Bello", 26, "bello", 700)
        p.text(887, ypos(values[-1][1])+7, "Raw", 26, "raw", 700)
    for x, (n, r, b) in zip(xlist, values):
        p.text(x, bottom+49, n, 21 if mobile else 26, weight=700, anchor="middle")
        p.text(x, bottom+76, f"+{b-r:.2f} pp", 15 if mobile else 19, "bello", 600, "middle")
    rule = 529 if mobile else 575
    p.line(p.margin, rule, p.w-p.margin, rule)
    p.text(p.margin, rule+40, "43.29 → 52.17%", 25 if mobile else 31, weight=700)
    p.text(p.w-p.margin, rule+40, "+8.88 pp", 27 if mobile else 35, "bello", 700, "end")
    p.text(p.margin, rule+66, "24 runs · 3 tasks · mean across all four models", 15 if mobile else 17, "muted")
    p.save()


def axis(p, x, width, y1, y2, ticks, maximum, suffix="", minimum=0):
    scale = lambda v: x + (v-minimum) / (maximum-minimum) * width
    for t in ticks:
        xx = scale(t)
        p.line(xx, y1, xx, y2)
        p.text(xx, y1-12, f"{t:g}{suffix}", 14 if p.mobile else 16, "muted", anchor="middle")
    return scale


def score_pair(p, y, raw, bello, scale, digits=2):
    p.line(scale(raw), y, scale(bello), y, "connector")
    p.point(scale(raw), y, "raw")
    p.point(scale(bello), y, "bello")
    p.text(scale(raw), y-15, f"{raw:.{digits}f}", 17 if p.mobile else 20, "raw halo", 600, "middle")
    p.text(scale(bello), y+29, f"{bello:.{digits}f}", 17 if p.mobile else 20, "bello halo", 700, "middle")


def bar_pair(p, x, y, raw, bello, scale, raw_label, bello_label):
    for v, label, yy, series in [(raw, raw_label, y-18, "raw"), (bello, bello_label, y+9, "bello")]:
        p.rect(x, yy-7, scale(v)-x, 13, series+"-bar")
        p.text(scale(v)+9, yy+6, label, 16 if p.mobile else 19, series, 600)


def budget(mobile):
    desc = "; ".join(f"{t}: score {r:.3f} to {b:.3f}%; weekly-limit use {u:.4f} to {v:.4f}%"
                     for t, r, b, u, v in BUDGET)
    p = Plate("programbench-efficient-budget-quality-cost", 1060 if mobile else 714,
              "Budget: 66.3% less weekly-limit usage, with average quality preserved",
              desc + ". Raw Sol XHigh versus Luna-based Bello Budget C+A. Three runs per task and arm. "
              "Mean score 48.100 to 48.797%. Total limit use 15.6297 to 5.2690%. "
              "Mean solution time 28:27 to 1:49:44.", mobile)
    p.header(3, ["Less usage.", "Average quality preserved."] if mobile else "Less usage. Average quality preserved.",
             "Budget C+A vs. Raw Sol XHigh · four tasks")
    sy = 195 if mobile else 163
    p.text(p.margin, sy, "−66.3%", 40 if mobile else 45, "bello", 700)
    p.text(p.margin, sy+26, "weekly-limit usage", 17 if mobile else 20, "muted")
    x2 = 268 if mobile else 565
    p.text(x2, sy, "+1.45%", 36 if mobile else 45, "bello", 700)
    p.text(x2, sy+26, "relative mean score", 17 if mobile else 20, "muted")
    p.legend(p.margin, sy+65, raw="Raw Sol", bello="Bello Budget", gap=175)
    if mobile:
        p.text(24, 310, "Completion score (%) ↑", 20, weight=700)
        s = axis(p, 161, 264, 352, 626, [30, 40, 50, 60], 65, minimum=30)
        for i, (t, r, b, u, v) in enumerate(BUDGET):
            y = 382+i*70
            task_label(p, 24, y+5, t)
            score_pair(p, y, r, b, s, 3)
        p.text(24, 677, "Weekly-limit usage (%) ↓", 20, weight=700)
        s = axis(p, 161, 229, 713, 973, [0, 2, 4, 6], 6.5)
        for i, (t, r, b, u, v) in enumerate(BUDGET):
            y = 749+i*70
            task_label(p, 24, y+3, t)
            bar_pair(p, 161, y, u, v, s, f"{u:.2f}", f"{v:.2f}")
        footer = 994
        p.line(24, footer, 456, footer)
        p.text(24, footer+27, "Usage: sum of 3 runs per task and arm.", 15, "muted")
        p.text(24, footer+50, "Mean solution time: 28:27 → 1:49:44.", 15, "muted")
    else:
        p.text(214, 284, "Completion score (%) ↑", 22, weight=700)
        p.text(672, 284, "Weekly-limit usage (%) ↓", 22, weight=700)
        s = axis(p, 218, 331, 326, 593, [30, 40, 50, 60], 65, minimum=30)
        u = axis(p, 674, 302, 326, 593, [0, 2, 4, 6], 6.5)
        for i, (t, r, b, a, z) in enumerate(BUDGET):
            y = 363+i*70
            task_label(p, 40, y+6, t)
            score_pair(p, y, r, b, s, 3)
            bar_pair(p, 674, y, a, z, u, f"{a:.2f}", f"{z:.2f}")
        p.line(40, 629, 1040, 629)
        p.text(40, 660, "Mean score  48.100 → 48.797%", 20, weight=600)
        p.text(1040, 660, "Total limit used  15.6297 → 5.2690%", 20, weight=600, anchor="end")
        p.text(40, 691, "Usage bars: sum of 3 runs per task / arm. Mean solution time: 28:27 → 1:49:44.", 17, "muted")
    p.save()


def task_label(p, x, y, task):
    if p.mobile and " " in task:
        a, b = task.split(" ", 1)
        p.text(x, y-8, a, 16, weight=600)
        p.text(x, y+13, b, 16, weight=600)
    else:
        p.text(x, y, task, 17 if p.mobile else 20, weight=600)


def hours(text):
    h, m, s = map(int, text.split(":"))
    return h + m/60 + s/3600


def ultra(mobile):
    rows = list(csv.DictReader((ROOT / "programbench_ca_run_info.csv").open()))
    desc = "; ".join(f"{r['task']}: score {r['raw_completion_pct']} to {r['ca_completion_pct']}%; "
                     f"time {r['raw_runtime']} to {r['ca_runtime']}" for r in rows)
    p = Plate("programbench-ca-performance", 926 if mobile else 639,
              "Sol Ultra C+A: better scores, longer runs", desc +
              ". Mean score 53.53 to 67.67%, +26.41% relative. Total solution time 2:48:55 to 7:08:06.", mobile)
    p.header(4, "What review adds.", ["Sol Ultra · completion review + adversary", "Solar, Samtools and rumdl"]
             if mobile else "Sol Ultra · completion review + adversary · three ProgramBench tasks")
    y = 186 if mobile else 164
    p.text(p.margin, y, "+26.41%", 40 if mobile else 46, "bello", 700)
    p.text(p.margin, y+28, "relative mean score", 18 if mobile else 20, "muted")
    p.text(285 if mobile else 565, y, "+14.14 pp", 26 if mobile else 38, "bello", 700)
    p.text(285 if mobile else 565, y+28, "53.53 → 67.67%", 17 if mobile else 21, "muted")
    p.legend(p.margin, y+68, raw="Raw Sol", bello="Bello C+A", gap=175)
    if mobile:
        p.text(24, 306, "Completion score (%) ↑", 20, weight=700)
        s = axis(p, 137, 288, 347, 551, [0, 25, 50, 75, 100], 100)
        for i, r in enumerate(rows):
            yy = 377+i*72
            task_label(p, 24, yy+5, r['task'])
            score_pair(p, yy, float(r['raw_completion_pct']), float(r['ca_completion_pct']), s)
        p.text(24, 603, "Solution time (hours) ↓", 20, weight=700)
        s = axis(p, 137, 253, 646, 844, [0, 1, 2, 3], 3)
        for i, r in enumerate(rows):
            yy = 680+i*72
            task_label(p, 24, yy+4, r['task'])
            bar_pair(p, 137, yy, hours(r['raw_runtime']), hours(r['ca_runtime']), s,
                     f"{hours(r['raw_runtime']):.2f}h", f"{hours(r['ca_runtime']):.2f}h")
        p.line(24, 864, 456, 864)
        p.text(24, 890, "Total solution time across the three tasks:", 16, "muted")
        p.text(24, 916, "2:48:55 → 7:08:06", 22, weight=700)
    else:
        p.text(213, 284, "Completion score (%) ↑", 22, weight=700)
        p.text(673, 284, "Solution time (hours) ↓", 22, weight=700)
        s = axis(p, 214, 326, 326, 525, [0, 25, 50, 75, 100], 100)
        t = axis(p, 674, 302, 326, 525, [0, 1, 2, 3], 3)
        for i, r in enumerate(rows):
            yy = 362+i*70
            task_label(p, 40, yy+5, r['task'])
            score_pair(p, yy, float(r['raw_completion_pct']), float(r['ca_completion_pct']), s)
            bar_pair(p, 674, yy, hours(r['raw_runtime']), hours(r['ca_runtime']), t,
                     f"{hours(r['raw_runtime']):.2f}h", f"{hours(r['ca_runtime']):.2f}h")
        p.line(40, 569, 1040, 569)
        p.text(40, 601, "Total solution time", 19, "muted")
        p.text(1040, 601, "2:48:55 → 7:08:06", 28, weight=700, anchor="end")
        p.text(40, 626, "Score is an unweighted task mean. Time bars show each run; the total is their sum.", 17, "muted")
    p.save()


def runtime(mobile):
    desc = "; ".join(f"{n}: {r:.2f} to {b:.2f}%, solution time {rt} to {bt}" for n,r,b,rt,bt in RUNTIME)
    p = Plate("runtime-only-custom-task-results", 727 if mobile else 640,
              "Runtime-only: score improved on all three custom tasks", desc +
              ". Each task has its own scoring criteria; there is no pooled score.", mobile)
    p.header(5, ["Supervision,", "without scheduled review."] if mobile else "Supervision, without scheduled review.",
             "Runtime-only · three custom tasks")
    p.legend(p.margin, 178 if mobile else 147, bello="Runtime on", gap=155)
    if mobile:
        p.text(24, 222, "Score (%) ↑", 18, "muted")
        s = axis(p, 145, 279, 259, 665, [0, 25, 50, 75, 100], 100)
        for i, (n, r, b, rt, bt) in enumerate(RUNTIME):
            y = 297+i*145
            p.text(24, y+5, n, 22, weight=700)
            score_pair(p, y, r, b, s)
            p.text(24, y+48, f"+{b-r:.2f} pp", 18, "bello", 700)
            p.text(145, y+64, f"Time  {rt} → {bt}", 16, "muted")
        p.line(24, 678, 456, 678)
        p.text(24, 709, "Task-specific scores; no pooled average.", 16, "muted")
    else:
        p.text(40, 189, "Score (%) ↑", 17, "muted")
        top, bottom = 225, 473
        yp = lambda v: bottom-v/100*(bottom-top)
        for v in [0,25,50,75,100]:
            p.line(86, yp(v), 1023, yp(v))
            p.text(69, yp(v)+5, v, 16, "muted", anchor="end")
        for i, (n, r, b, rt, bt) in enumerate(RUNTIME):
            x = 164+i*320
            p.text(x+87, 194, n, 27, weight=700, anchor="middle")
            p.line(x, yp(r), x+174, yp(b), "bello-line")
            p.point(x, yp(r), "raw", 7)
            p.point(x+174, yp(b), "bello", 7)
            p.text(x, yp(r)+30, f"{r:.2f}", 22, "raw halo", 700, "middle")
            p.text(x+174, yp(b)-16, f"{b:.2f}", 22, "bello halo", 700, "middle")
            p.text(x+87, 521, f"+{b-r:.2f} pp", 28, "bello", 700, "middle")
            p.text(x+87, 552, f"{rt} → {bt}", 19, "muted", anchor="middle")
        p.line(40, 590, 1040, 590)
        p.text(40, 621, "Task-specific scores; no pooled average. Times below each panel are solution times.", 18, "muted")
    p.save()


def distiller(mobile):
    p = Plate("readme-distiller", 771 if mobile else 675,
              "Log distiller: estimated usage reduction on JSON Schema",
              "Astra XHigh: 3 runs per arm, 22.02% estimated usage reduction, score 62.22 to 60.57%, "
              "solution time 33:04 to 42:10; runtime off in both arms. Luna Max: 6 runs per arm, "
              "14.78% estimated usage reduction including runtime, score 56.75 to 55.45%, time "
              "1:41:53 to 1:22:38. Subtracting recorded runtime cost gives 25.28% lower coder cost. "
              "This is accounting, not a separate runtime-off run. Usage is API-equivalent, not a measured weekly counter.", mobile)
    p.header(2, ["Shorter logs.", "Lower usage."] if mobile else "Shorter logs. Lower usage.",
             "JSON Schema · estimated usage reduction")
    p.legend(p.margin, 178 if mobile else 145, raw="Raw = 100", bello="With distiller", gap=190)
    # Two exact normalized comparisons. Empty right-hand spans are savings, not confidence intervals.
    groups = [
        ("Astra XHigh", "3 runs / arm · runtime off", 22.02313172, "62.22 → 60.57%", "33:04 → 42:10"),
        ("Luna Max", "6 runs / arm · runtime included", 14.77577457, "56.75 → 55.45%", "1:41:53 → 1:22:38"),
    ]
    for i, (name, subtitle, saved, score, time) in enumerate(groups):
        y = (226+i*237) if mobile else (216+i*194)
        x = p.margin
        p.text(x, y, name, 26 if mobile else 28, weight=700)
        p.text(x, y+26, subtitle, 15 if mobile else 17, "muted")
        p.text(p.w-p.margin, y+3, f"−{saved:.2f}%", 33 if mobile else 44, "bello", 700, "end")
        bx = x if mobile else 334
        by = y+61 if mobile else y-1
        bw = 428 if mobile else 522
        p.rect(bx, by, bw, 14, "raw-bar")
        p.rect(bx, by+30, bw*(100-saved)/100, 20, "bello-bar")
        p.line(bx+bw*(100-saved)/100, by+40, bx+bw, by+40, "connector", 'stroke-dasharray="3 5"')
        p.line(bx+bw, by-3, bx+bw, by+57, "grid")
        # Direct labels survive without colour and without consulting an axis.
        if not mobile:
            p.text(bx-12, by+14, "100", 17, "raw", 600, "end")
            p.text(bx-12, by+46, f"{100-saved:.2f}", 17, "bello", 600, "end")
        stats_y = y+145 if mobile else y+89
        p.text(x, stats_y, f"Score  {score}", 18 if mobile else 20, weight=600)
        p.text(x if mobile else 550, stats_y+(26 if mobile else 0), f"Time  {time}", 17 if mobile else 20, "muted")
        if i == 0:
            p.line(p.margin, y+(205 if mobile else 136), p.w-p.margin, y+(205 if mobile else 136))
    yy = 697 if mobile else 580
    p.line(p.margin, yy-20, p.w-p.margin, yy-20)
    p.text(p.margin, yy+10, "25.28%", 29 if mobile else 34, "bello", 700)
    p.text(161 if mobile else 199, yy+8, "Luna: subtract runtime cost", 17 if mobile else 22, weight=600)
    p.text(p.margin, yy+40, "Accounting subtraction, not an extra runtime-off run.", 15 if mobile else 18, "muted")
    if not mobile:
        p.text(p.margin, yy+69, "Bars normalize each Raw baseline to 100. Estimates use recorded tokens at API-equivalent rates.", 17, "muted")
    p.save()


def main():
    for mobile in [False, True]:
        model_comparison(mobile)
        distiller(mobile)
        budget(mobile)
        ultra(mobile)
        runtime(mobile)
    print("Rendered 5 benchmark figures × 2 layouts.")


if __name__ == "__main__":
    main()
