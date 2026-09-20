#!/usr/bin/env python3
"""Generate minimal README charts (wide/mobile; light/dark), without dependencies.

Run: python3 scripts/plot_readme_graphs.py
Data and source links: docs/assets/benchmark-graphs.md.
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
    ("Marl", 32.91, 37.91),
    ("Slab", 81.08, 85.69),
    ("Pinch", 89.25, 98.00),
]
DISTILLER = [
    # Model, raw / distiller API-equivalent mean cost, raw / distiller mean score.
    ("Astra XHigh", 10.31219067, 8.04112333, 62.22146430, 60.57298772),
    ("Luna Max", 1.71064142, 1.45788090, 56.74738518, 55.45134152),
]

CSS = """
svg { --bg:#fff; --ink:#26323e; --muted:#64707c; --grid:#e4e9ee;
  --raw:#557ca6; --bello:#c84b32; }
@media (prefers-color-scheme:dark) {
  svg { --bg:#0d1117; --ink:#edf2f7; --muted:#a6b2bf; --grid:#2b3542;
    --raw:#8dadd2; --bello:#ef987e; }
}
.paper { fill:var(--bg); }
text { font-family:Arial,Helvetica,sans-serif; fill:var(--ink);
  font-variant-numeric:tabular-nums; }
.muted { fill:var(--muted); }
.raw { fill:var(--raw); }
.bello { fill:var(--bello); }
.halo { paint-order:stroke; stroke:var(--bg); stroke-width:5; stroke-linejoin:round; }
.grid { stroke:var(--grid); stroke-width:1; }
.raw-line { stroke:var(--raw); fill:none; stroke-width:2.5; stroke-dasharray:7 5; }
.bello-line { stroke:var(--bello); fill:none; stroke-width:3; }
.raw-point { fill:var(--bg); stroke:var(--raw); stroke-width:2; }
.bello-point { fill:var(--bello); }
.raw-bar { fill:var(--raw); fill-opacity:.22; stroke:var(--raw); stroke-width:1.3; }
.bello-bar { fill:var(--bello); }
"""


class Chart:
    def __init__(self, name, height, title, desc, mobile):
        self.mobile = mobile
        self.name = name + ("-mobile" if mobile else "")
        self.w, self.h = (480 if mobile else 1080), height
        self.parts = [
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{self.w}" height="{height}" '
            f'viewBox="0 0 {self.w} {height}" role="img" '
            f'aria-labelledby="{self.name}-title {self.name}-desc">',
            f'<title id="{self.name}-title">{escape(title)}</title>',
            f'<desc id="{self.name}-desc">{escape(desc)}</desc>',
            f'<style>{CSS}</style>',
            f'<rect class="paper" width="{self.w}" height="{height}"/>',
        ]

    def text(self, x, y, value, size=18, cls="", anchor="start", weight=400):
        self.parts.append(f'<text x="{x:.2f}" y="{y:.2f}" font-size="{size}" '
                          f'font-weight="{weight}" class="{cls}" text-anchor="{anchor}">'
                          f'{escape(str(value))}</text>')

    def line(self, x1, y1, x2, y2, cls="grid"):
        self.parts.append(f'<line x1="{x1:.2f}" y1="{y1:.2f}" x2="{x2:.2f}" '
                          f'y2="{y2:.2f}" class="{cls}"/>')

    def rect(self, x, y, w, h, cls):
        self.parts.append(f'<rect x="{x:.2f}" y="{y:.2f}" width="{w:.2f}" '
                          f'height="{h:.2f}" class="{cls}"/>')

    def point(self, x, y, series):
        if series == "raw":
            self.parts.append(f'<circle cx="{x:.2f}" cy="{y:.2f}" r="5" class="raw-point"/>')
        else:
            self.parts.append(f'<path d="M{x:.2f} {y-6:.2f} l6 6 l-6 6 l-6 -6 Z" class="bello-point"/>')

    def legend(self, raw="Raw", bello="Bello", lines=False):
        x, y = (24, 30) if self.mobile else (650, 30)
        gap = 220 if self.mobile else 200
        for i, (label, series) in enumerate([(raw, "raw"), (bello, "bello")]):
            xx = x + i*gap
            if lines:
                self.line(xx, y-6, xx+24, y-6, series+"-line")
                self.point(xx+12, y-6, series)
            else:
                self.rect(xx, y-14, 16, 13, series+"-bar")
            self.text(xx+30, y, label, 17 if self.mobile else 19, series)

    def save(self):
        (OUT / f"{self.name}.svg").write_text("\n".join(self.parts+["</svg>"])+"\n", encoding="utf-8")


def mean(values):
    return sum(values)/len(values)


def model_comparison(mobile):
    values = [(n, mean(r), mean(b)) for n,r,b in MODELS]
    desc = "; ".join(f"{n}: Raw {r:.2f}%, Bello {b:.2f}%" for n,r,b in values)
    p = Chart("readme-model-comparison", 430 if mobile else 452,
              "Mean ProgramBench score, Raw versus Bello",
              desc+". Mean across Solar, Samtools and rumdl. 24 total runs. Score axis 25–72%.", mobile)
    p.legend(lines=True)
    p.text(24 if mobile else 42, 73 if mobile else 31, "Score (%)", 18, "muted")
    left, right, top, bottom = (46,453,110,364) if mobile else (78,1024,77,382)
    xs = [72,190,308,426] if mobile else [132,410,688,966]
    yp = lambda v: bottom-(v-25)/47*(bottom-top)
    for v in [30,50,70]:
        p.line(left,yp(v),right,yp(v))
        p.text(left-10,yp(v)+5,v,14 if mobile else 17,"muted","end")
    for idx,series in [(1,"raw"),(2,"bello")]:
        points=" ".join(f"{x:.2f},{yp(row[idx]):.2f}" for x,row in zip(xs,values))
        p.parts.append(f'<polyline points="{points}" class="{series}-line"/>')
        for x,row in zip(xs,values):
            val=row[idx]
            p.point(x,yp(val),series)
            p.text(x,yp(val)+(27 if series=="raw" else -16),f"{val:.2f}",
                   18 if mobile else 22,series+" halo","middle",600)
    for x,(name,_,__) in zip(xs,values):
        p.text(x,bottom+44,name,20 if mobile else 23,anchor="middle")
    p.save()


def panel(p, x, y, width, height, title, names, raw, bello, maximum, ticks,
          raw_labels, bello_labels, precision_size=17):
    p.text(x,y,title,19 if p.mobile else 21)
    top,bottom=y+42,y+42+height
    scale=lambda v: bottom-v/maximum*height
    for tick in ticks:
        p.line(x,scale(tick),x+width,scale(tick))
        p.text(x-10,scale(tick)+5,f"{tick:g}",14 if p.mobile else 16,"muted","end")
    group=width/len(names)
    half_gap=25 if len(names)==4 else 33
    bar_width=28 if len(names)==4 else 38
    for i,name in enumerate(names):
        center=x+group*(i+.5)
        for value,label,offset,series in [
            (raw[i],raw_labels[i],-half_gap,"raw"),
            (bello[i],bello_labels[i],half_gap,"bello"),
        ]:
            xx=center+offset
            p.rect(xx-bar_width/2,scale(value),bar_width,bottom-scale(value),series+"-bar")
            p.text(xx,scale(value)-10,label,precision_size,series,"middle",500)
        label_lines=name.split(" ") if " " in name else [name]
        for j,line in enumerate(label_lines):
            p.text(center,bottom+31+j*21,line,16 if p.mobile else 18,anchor="middle")


def two_panel_geometry(mobile):
    if mobile:
        return [(49,80,407,238),(49,455,407,238)]
    return [(63,85,432,284),(610,85,432,284)]


def budget(mobile):
    desc="; ".join(f"{n}: score {r:.3f} to {b:.3f}%; weekly limit {u:.4f} to {v:.4f}%"
                  for n,r,b,u,v in BUDGET)
    p=Chart("programbench-efficient-budget-quality-cost",815 if mobile else 475,
            "Budget C+A versus Raw Sol XHigh",desc+
            ". Score: mean of 3 runs per task and arm. Usage: sum of those 3 runs. "
            "Overall score 48.100 to 48.797%; total usage 15.6297 to 5.2690%.",mobile)
    p.legend("Raw Sol XHigh","Bello Budget")
    a,b=two_panel_geometry(mobile)
    names=[r[0] for r in BUDGET]
    panel(p,*a,"Mean score (%)",names,[r[1] for r in BUDGET],[r[2] for r in BUDGET],
          70,[0,35,70],[f"{r[1]:.2f}" for r in BUDGET],[f"{r[2]:.2f}" for r in BUDGET],17)
    panel(p,*b,"Weekly limit used (%)",names,[r[3] for r in BUDGET],[r[4] for r in BUDGET],
          7,[0,3.5,7],[f"{r[3]:.2f}" for r in BUDGET],[f"{r[4]:.2f}" for r in BUDGET],17)
    p.save()


def hours(text):
    h,m,s=map(int,text.split(":"))
    return h+m/60+s/3600


def ultra(mobile):
    with (ROOT/"programbench_ca_run_info.csv").open() as f:
        rows=list(csv.DictReader(f))
    desc="; ".join(f"{r['task']}: score {r['raw_completion_pct']} to {r['ca_completion_pct']}%; "
                  f"time {r['raw_runtime']} to {r['ca_runtime']}" for r in rows)
    p=Chart("programbench-ca-performance",793 if mobile else 453,
            "Sol Ultra, Raw versus completion review and adversary",desc,mobile)
    p.legend("Raw Sol Ultra","Bello C+A")
    a,b=two_panel_geometry(mobile)
    names=[r["task"] for r in rows]
    panel(p,*a,"Score (%)",names,[float(r["raw_completion_pct"]) for r in rows],
          [float(r["ca_completion_pct"]) for r in rows],100,[0,50,100],
          [r["raw_completion_pct"] for r in rows],[r["ca_completion_pct"] for r in rows])
    panel(p,*b,"Solution time (h)",names,[hours(r["raw_runtime"]) for r in rows],
          [hours(r["ca_runtime"]) for r in rows],3,[0,1.5,3],
          [r["raw_runtime"].lstrip("0").lstrip(":") for r in rows],
          [r["ca_runtime"].lstrip("0").lstrip(":") for r in rows])
    p.save()


def runtime(mobile):
    desc="; ".join(f"{n}: Raw {r:.2f}%, runtime-only {b:.2f}%" for n,r,b in RUNTIME)
    p=Chart("runtime-only-custom-task-results",422 if mobile else 448,
            "Raw versus runtime-only",desc+". Each task has its own scoring criteria.",mobile)
    p.legend("Raw","Runtime on")
    panel(p,49 if mobile else 76,80,407 if mobile else 940,238 if mobile else 268,
          "Score (%)",[r[0] for r in RUNTIME],[r[1] for r in RUNTIME],[r[2] for r in RUNTIME],
          100,[0,50,100],[f"{r[1]:.2f}" for r in RUNTIME],[f"{r[2]:.2f}" for r in RUNTIME],17 if mobile else 21)
    p.save()


def distiller(mobile):
    desc="; ".join(
        f"{name}: Raw cost index 100, score {rs:.2f}%; distiller cost index "
        f"{100*dc/rc:.2f}, score {ds:.2f}%"
        for name,rc,dc,rs,ds in DISTILLER)
    p=Chart("readme-distiller",489 if mobile else 467,
            "Log distiller: API-equivalent cost versus quality",desc+
            ". JSON Schema. Astra has 3 runs per arm and runtime off. Luna has 6 runs per arm; "
            "its distiller arm includes runtime. API-equivalent cost: Raw = 100 for each model. "
            "Score axis 50–65%; cost axis 70–105. Lower cost and higher score are better.",mobile)
    p.parts.append('<style>.cost-link { stroke:var(--muted); stroke-opacity:.55; stroke-width:1.5; }</style>')
    # The connector pairs the two observations for each model; it is not a fit.
    p.legend("Raw","Distiller",lines=True)
    p.text(24 if mobile else 42,73 if mobile else 31,"Score (%) ↑",18,"muted")
    left,right,top,bottom=(55,445,109,383) if mobile else (88,1024,80,370)
    xp=lambda v: left+(v-70)/35*(right-left)
    yp=lambda v: bottom-(v-50)/15*(bottom-top)
    for tick in [50,55,60,65]:
        p.line(left,yp(tick),right,yp(tick))
        p.text(left-11,yp(tick)+5,tick,14 if mobile else 17,"muted","end")
    for tick in [75,85,100]:
        p.line(xp(tick),top,xp(tick),bottom)
        p.text(xp(tick),bottom+27,tick,15 if mobile else 18,"muted","middle")
    for i,(name,rc,dc,rs,ds) in enumerate(DISTILLER):
        cost=100*dc/rc
        raw_x,raw_y=xp(100),yp(rs)
        dist_x,dist_y=xp(cost),yp(ds)
        p.line(raw_x,raw_y,dist_x,dist_y,"cost-link")
        p.point(raw_x,raw_y,"raw")
        p.point(dist_x,dist_y,"bello")
        p.text(raw_x,raw_y-17,f"{rs:.2f}",18 if mobile else 22,"raw halo","middle",600)
        p.text(dist_x,dist_y+29,f"{ds:.2f}",18 if mobile else 22,"bello halo","middle",600)
        mid_x=(raw_x+dist_x)/2-(14 if mobile else 0)
        mid_y=(raw_y+dist_y)/2
        p.text(mid_x,mid_y-(27 if i==0 else 50),
               f"{name} · −{100-cost:.2f}% cost",16 if mobile else 22,"halo","middle",500)
        if i==1:
            p.text(mid_x,mid_y-22,"runtime included",14 if mobile else 17,"muted halo","middle")
    p.text((left+right)/2,bottom+65,"← API-equivalent cost (Raw = 100 per model)",
           16 if mobile else 20,"muted","middle")
    p.save()


def main():
    for mobile in [False,True]:
        for render in [model_comparison,distiller,budget,ultra,runtime]:
            render(mobile)
    print("Rendered 5 minimal charts × 2 layouts.")


if __name__=="__main__":
    main()
