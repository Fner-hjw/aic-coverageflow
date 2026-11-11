#!/usr/bin/env python3
"""Generate docs/workflow-diagram.png, the figure in docs/design-note.md.

The figure shows one coverage update as a left-to-right flow across three
swimlanes: the analyst, the Python controller, and the model calls (each in a
fresh context). Controller steps are white boxes, model calls are teal,
analyst steps are tan. After the gates, four critics review in parallel, the
controller merges findings and decides: two clean reviews lead to analyst
approval and publish, build-side findings loop back along the bottom to the
earliest affected stage, engine/harness/mandate findings block, and a stalled
run escalates to the analyst.

Pure matplotlib, deterministic output, 2400x1350 px.

Usage:  python scripts/gen_diagram.py [--out docs/workflow-diagram.png]
"""

from __future__ import annotations

import argparse
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib import font_manager  # noqa: E402
from matplotlib.patches import FancyBboxPatch, Polygon  # noqa: E402

W, H, DPI = 2400, 1350, 200          # canvas in pixels; 1 data unit = 1 px
PT = 72 / DPI                        # pixels -> points

INK = "#222222"
GREY = "#777777"
RULE = "#D9D9D9"
CODE = dict(fc="#FFFFFF", ec="#444444")
MODEL = dict(fc="#E6F0EE", ec="#2F6F68")
ANALYST = dict(fc="#F6EFE6", ec="#8A5A2B")
BLOCKED = dict(fc="#FFFFFF", ec="#9B3D3D")
RED = "#9B3D3D"

NAME_PX, SUB_PX, NOTE_PX = 20, 16, 15

# Lane bands (top, bottom) and the row each lane's boxes sit on.
LANES = {"analyst": (85, 360), "ctrl": (360, 700), "model": (700, 1100)}
LANE_LABELS = {"analyst": "Analyst", "ctrl": "Controller (Python)",
               "model": "Model calls\n(fresh context)"}
Y_CTRL, Y_MODEL = 545, 890

# Layout table: key -> (style, cx, cy, w, h, name, sub)
BOXES = {
    "prefill":  (CODE,    295, Y_CTRL, 200, 84, "Prefill mandate", "SEC filings, prices, peers"),
    "review":   (ANALYST, 295, 225,    200, 84, "Review and accept", "edits recorded"),
    "ingest":   (CODE,    505, Y_CTRL, 200, 84, "Ingest and slice", "filings"),
    "extract":  (MODEL,   610, Y_MODEL, 200, 84, "Extractor", "facts with citations"),
    "engine":   (CODE,    715, Y_CTRL, 200, 84, "Model engine", ""),
    "role":     (MODEL,   820, Y_MODEL, 200, 84, "Model role", "growth, margin, capex"),
    "valuation": (CODE,   925, Y_CTRL, 200, 84, "Valuation engine", "LTM base, DCF, target"),
    "writer":   (MODEL,  1046, Y_MODEL, 224, 84, "Writer", "note with evidence markers"),
    "gates":    (CODE,   1165, Y_CTRL, 230, 150, "Gates",
                 "cutoff, schema, periods,\nbounds, recompute,\nterminal check,\nevidence links, controls"),
    "merge":    (CODE,   1405, Y_CTRL, 200, 84, "Merge findings", "ledger, verified closures"),
    "approve":  (ANALYST, 2010, 160,   200, 76, "Approve", ""),
    "escalate": (ANALYST, 2010, 280,   200, 100, "Escalated", "publish with exceptions\nor reject"),
    "publish":  (CODE,   2265, 445,    200, 84, "Publish", "note + clean export"),
    "blocked":  (BLOCKED, 2010, Y_CTRL, 200, 84, "Blocked", "evolution input"),
}
CRITICS = ["C1 evidence", "C2 robustness", "C3 timing", "C4 controls"]
CRIT_CX, CRIT_W, CRIT_H = 1310, 160, 52
CRIT_Y = [791 + 66 * i for i in range(4)]
DECIDE = (1600, Y_CTRL, 110, 100)    # diamond: cx, cy, w, h
TRUNK_X = 1745
Y_REPAIR = 1160


def pick_font() -> str:
    have = {f.name for f in font_manager.fontManager.ttflist}
    for name in ("Helvetica Neue", "Helvetica", "Arial", "DejaVu Sans"):
        if name in have:
            return name
    return "DejaVu Sans"


# ------------------------------------------------------------- primitives
def edge(key, side, dx=0):
    """Anchor on a box edge: side in top/bottom/left/right."""
    _, cx, cy, w, h, *_ = BOXES[key]
    return {"top": (cx + dx, cy - h / 2), "bottom": (cx + dx, cy + h / 2),
            "left": (cx - w / 2, cy), "right": (cx + w / 2, cy)}[side]


class Fig:
    def __init__(self, font: str):
        self.font = font
        self.fig = plt.figure(figsize=(W / DPI, H / DPI), dpi=DPI)
        self.ax = self.fig.add_axes([0, 0, 1, 1])
        self.ax.set_xlim(0, W)
        self.ax.set_ylim(H, 0)
        self.ax.axis("off")
        self.fig.patch.set_facecolor("white")
        self.fitted = []             # (label, text artists, box rect) for the overflow check

    def text(self, x, y, s, px, color=INK, weight="normal", ha="center", va="center", **kw):
        return self.ax.text(x, y, s, fontsize=px * PT, color=color, fontweight=weight,
                            ha=ha, va=va, family=self.font, linespacing=1.25, **kw)

    def rbox(self, cx, cy, w, h, style):
        self.ax.add_patch(FancyBboxPatch(
            (cx - w / 2, cy - h / 2), w, h, boxstyle="round,pad=0,rounding_size=4",
            fc=style["fc"], ec=style["ec"], lw=1.2 * PT, zorder=3))

    def box(self, cx, cy, w, h, style, name, sub="", name_color=INK):
        self.rbox(cx, cy, w, h, style)
        n_sub = sub.count("\n") + 1 if sub else 0
        block = 28 + n_sub * 21
        top = cy - block / 2
        arts = [self.text(cx, top + 12, name, NAME_PX, name_color, 600, zorder=4)]
        if sub:
            arts.append(self.text(cx, top + 28 + n_sub * 10.5, sub, SUB_PX, "#444444", zorder=4))
        self.fitted.append((name, arts, (cx - w / 2, cx + w / 2, cy - h / 2, cy + h / 2)))

    def line(self, pts, color="#444444", dashed=False):
        xs, ys = zip(*pts)
        self.ax.plot(xs, ys, color=color, lw=1.3 * PT, solid_capstyle="butt",
                     solid_joinstyle="miter", zorder=2, ls=(0, (4, 3)) if dashed else "-")

    def arrow(self, pts, color="#444444", dashed=False):
        """Orthogonal polyline with an arrowhead on the last segment."""
        if len(pts) > 2:
            self.line(pts[:-1], color, dashed)
        (x0, y0), (x1, y1) = pts[-2:]
        if dashed:                   # keep the head's own shaft short so the dashes show
            d = max(abs(x1 - x0), abs(y1 - y0))
            x0, y0 = x1 + (x0 - x1) * 12 / d, y1 + (y0 - y1) * 12 / d
        self.ax.annotate("", xy=pts[-1], xytext=(x0, y0), zorder=2, arrowprops=dict(
            arrowstyle="-|>,head_length=0.5,head_width=0.2", color=color, lw=1.3 * PT,
            shrinkA=0, shrinkB=0, mutation_scale=9))
        # straight line body for the final segment
        self.line(pts[-2:], color, dashed)

    def check_fit(self):
        self.fig.canvas.draw()
        r = self.fig.canvas.get_renderer()
        inv = self.ax.transData.inverted()
        for label, arts, (x0, x1, y0, y1) in self.fitted:
            for a in arts:
                bb = a.get_window_extent(r)
                (ax0, ay0), (ax1, ay1) = inv.transform((bb.x0, bb.y0)), inv.transform((bb.x1, bb.y1))
                if min(ax0, ax1) < x0 + 8 or max(ax0, ax1) > x1 - 8 or \
                        min(ay0, ay1) < y0 + 4 or max(ay0, ay1) > y1 - 4:
                    print(f"WARNING: text overflows box '{label}': {a.get_text()!r}")


# ----------------------------------------------------------------- figure
def draw(f: Fig) -> None:
    # title and lanes
    f.text(30, 42, "CoverageFlow: one coverage update", 26, INK, 600, ha="left")
    for lane, (top, bot) in LANES.items():
        f.line([(30, bot), (W - 30, bot)], RULE)
        f.text(30, (top + bot) / 2 - (0 if lane != "analyst" else 0), LANE_LABELS[lane], 17, GREY, ha="left")
    f.line([(30, LANES["analyst"][0]), (W - 30, LANES["analyst"][0])], RULE)

    for k, (style, cx, cy, w, h, name, sub) in BOXES.items():
        f.box(cx, cy, w, h, style, name, sub, RED if k == "blocked" else INK)

    # critics (parallel, each fed only its own inputs)
    for name, cy in zip(CRITICS, CRIT_Y):
        f.box(CRIT_CX, cy, CRIT_W, CRIT_H, MODEL, name)
    f.text(CRIT_CX, 1052, "run in parallel,\neach with only its own inputs", NOTE_PX, GREY)

    # decision diamond
    dx, dy, dw, dh = DECIDE
    f.ax.add_patch(Polygon([(dx - dw / 2, dy), (dx, dy - dh / 2), (dx + dw / 2, dy), (dx, dy + dh / 2)],
                           closed=True, fc="white", ec="#444444", lw=1.2 * PT, zorder=3))
    f.text(dx, dy, "Decide", NAME_PX, INK, 600, zorder=4)

    # mandate
    f.arrow([edge("prefill", "top"), edge("review", "bottom")])
    rx, ry = edge("review", "right")
    f.arrow([(rx, ry), (505, ry), edge("ingest", "top")])

    # build staircase: vertical arrows through the overlap of neighbouring boxes
    f.arrow([edge("ingest", "bottom", 53), edge("extract", "top", -52)])
    f.arrow([edge("extract", "top", 52), edge("engine", "bottom", -53)])
    f.arrow([edge("engine", "bottom", 53), edge("role", "top", -52)])
    f.arrow([edge("role", "top", 52), edge("valuation", "bottom", -53)])
    f.arrow([edge("valuation", "bottom", 53), edge("writer", "top", 978 - 1046)])
    f.arrow([edge("writer", "top", 44), edge("gates", "bottom", -75)])

    # writer retry loop
    wx, wy = edge("writer", "bottom")
    f.line([(wx + 40, wy), (wx + 40, wy + 30), (wx - 30, wy + 30)])
    f.arrow([(wx - 30, wy + 30), (wx - 30, wy)])
    f.text(wx + 5, wy + 62, "unlinked number: writer retries\n(no round used)", NOTE_PX, GREY)

    # gates -> critics (fan out) -> merge (fan in)
    gx = 1190
    f.line([(gx, edge("gates", "bottom")[1]), (gx, CRIT_Y[-1])])
    cl, cr = CRIT_CX - CRIT_W / 2, CRIT_CX + CRIT_W / 2
    for cy in CRIT_Y:
        f.arrow([(gx, cy), (cl, cy)])
        f.line([(cr, cy), (1415, cy)])
    f.arrow([(1415, CRIT_Y[-1]), edge("merge", "bottom", 10)])

    # merge -> decide
    f.arrow([edge("merge", "right"), (dx - dw / 2, dy)])

    # outcomes
    f.line([(dx + dw / 2, dy), (TRUNK_X, dy)])
    f.line([(TRUNK_X, 160), (TRUNK_X, Y_REPAIR), (455, Y_REPAIR)])
    f.arrow([(TRUNK_X, 160), edge("approve", "left")])
    f.arrow([(TRUNK_X, 280), edge("escalate", "left")])
    f.arrow([(TRUNK_X, dy), edge("blocked", "left")])
    ax_, ay_ = edge("approve", "right")
    f.arrow([(ax_, ay_), (2265, ay_), edge("publish", "top")])
    ex, ey = edge("escalate", "bottom")
    px, py = edge("publish", "left")
    f.arrow([(ex, ey), (ex, py), (px, py)], dashed=True)
    f.text(ex + 12, py - 40, "with exceptions\nbanner", NOTE_PX, GREY, ha="left")
    f.arrow([(455, Y_REPAIR), (455, edge("ingest", "bottom")[1])])

    lx = TRUNK_X + 12
    f.text(lx, 142, "2 clean reviews", NOTE_PX, GREY, ha="left")
    f.text(lx, 262, "stalled or out\nof rounds", NOTE_PX, GREY, ha="left")
    f.text(lx, dy - 24, "engine, harness or\nmandate findings", NOTE_PX, GREY, ha="left")
    f.text(lx, 840, "writer, model or\nextractor findings", NOTE_PX, GREY, ha="left")
    f.text(480, Y_REPAIR - 18, "repair from earliest affected stage", NOTE_PX, GREY, ha="left")

    # footer
    f.line([(30, 1255), (W - 30, 1255)], RULE)
    f.text(30, 1292, "Every step is recorded in state.json, trace.jsonl and the findings ledger. "
           "Prompt cards, mapping tables and engines change only through the evolution loop, "
           "with human approval.", 17, GREY, ha="left")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(Path(__file__).resolve().parent.parent / "docs" / "workflow-diagram.png"))
    args = ap.parse_args()
    f = Fig(pick_font())
    draw(f)
    f.check_fit()
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    f.fig.savefig(args.out, dpi=DPI, facecolor="white", metadata={"Software": None})
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
