#!/usr/bin/env python3
"""Generate docs/workflow-diagram.png (owner: stream A5, docs & evidence).

Renders the CoverageFlow workflow figure embedded in docs/design-note.md:
the lecture's slide-4 architecture adapted to this project (mandate ->
Orchestrator -> Generator build team -> numerical gates -> Critic review
team -> merge -> repair-packet loop -> 2-clean acceptance -> human
approval -> published version), plus the evolution boundary (version
registry) and the shared state / evidence layer.

Pure matplotlib (no exotic dependencies). Institutional look: serif type,
restrained palette, thin rules. Deterministic output (no timestamps).

Usage:  python scripts/gen_diagram.py [--out docs/workflow-diagram.png]
"""

from __future__ import annotations

import argparse
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib import font_manager  # noqa: E402
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch, Rectangle  # noqa: E402

# ---------------------------------------------------------------- palette
PAPER = "#FBFAF7"          # page background
INK = "#1F2A36"            # primary text / rules
NAVY = "#2E4A66"           # orchestrator / control accents
STEEL = "#5C7186"          # secondary text
FAINT = "#C9C3B8"          # hairlines
GEN_FILL = "#F1EEE5"       # generator boxes
GEN_EDGE = "#8C7A56"       # generator accent (bronze)
CRI_FILL = "#ECEFF2"       # critic boxes
CRI_EDGE = "#41586E"       # critic accent (steel blue)
HUM_EDGE = "#7A4A3A"       # human gate accent (muted oxblood)
EVO_FILL = "#F4F4F0"
EVO_EDGE = "#6E7B66"       # evolution accent (muted olive)

W, H = 15.0, 12.4          # canvas in inches (1 unit = 1 inch)
XC = 9.0                   # main column center
SERIF = "serif"            # resolved in main()


def _serif() -> str:
    """Prefer a real serif installed on the machine; fall back to DejaVu."""
    prefer = ["Times New Roman", "Times", "STIXGeneral", "Nimbus Roman",
              "Liberation Serif", "DejaVu Serif"]
    available = {f.name for f in font_manager.fontManager.ttflist}
    for name in prefer:
        if name in available:
            return name
    return "serif"


def box(ax, x, y, w, h, *, title, lines=(), edge, face, title_size,
        line_size=None, lw=0.9, subtitle=None, title_color=None,
        ls="solid"):
    """Draw one labeled box (title optional when lines are given)."""
    p = FancyBboxPatch((x, y), w, h, boxstyle="square,pad=0", linewidth=lw,
                       edgecolor=edge, facecolor=face, linestyle=ls, zorder=2)
    ax.add_patch(p)
    line_size = line_size or max(6.4, title_size - 1.5)
    tc = title_color or INK
    cx = x + w / 2
    ty = y + h - 0.27
    ax.text(cx, ty, title, ha="center", va="center", fontsize=title_size,
            family=SERIF, color=tc, fontweight="bold", zorder=3)
    if subtitle:
        ax.text(cx, ty - 0.21, subtitle, ha="center", va="center",
                fontsize=line_size, family=SERIF, color=STEEL, style="italic",
                zorder=3)
        top = ty - 0.40
    else:
        top = ty - 0.24
    for i, ln in enumerate(lines):
        ax.text(cx, top - i * 0.175, ln, ha="center", va="center",
                fontsize=line_size, family=SERIF, color=STEEL, zorder=3)
    return p


def arrow(ax, x1, y1, x2, y2, *, edge=INK, lw=1.1, ls="solid", alpha=1.0,
          zorder=4):
    a = FancyArrowPatch((x1, y1), (x2, y2), arrowstyle="-|>", mutation_scale=12,
                        linewidth=lw, color=edge, shrinkA=2, shrinkB=2,
                        linestyle=ls, alpha=alpha, zorder=zorder)
    ax.add_patch(a)
    return a


def note(ax, x, y, text, *, size=6.2, color=STEEL, ha="center", ma="center",
         weight="normal", style="normal", mask=True, rotation=0):
    ax.text(x, y, text, ha=ha, va="center", fontsize=size, family=SERIF,
            color=color, zorder=6, fontweight=weight, style=style, rotation=rotation,
            multialignment=ma,
            bbox=(dict(facecolor=PAPER, edgecolor="none", pad=1.2,
                       alpha=0.92) if mask else None))


def render(out: Path) -> Path:
    fig, ax = plt.subplots(figsize=(W, H), dpi=200)
    fig.patch.set_facecolor(PAPER)
    ax.set_xlim(0, W)
    ax.set_ylim(0, H)
    ax.axis("off")

    # ---------------------------------------------------------------- title
    ax.text(XC, H - 0.28, "CoverageFlow — Coverage-Update Workflow",
            ha="center", va="center", fontsize=16, family=SERIF, color=INK,
            fontweight="bold")
    ax.text(XC, H - 0.58,
            "Orchestrator = deterministic code · Generator / Critic = role "
            "dispatches (fresh context per call) · repair loop · human "
            "approval · evolution under regression testing",
            ha="center", va="center", fontsize=7.4, family=SERIF, color=STEEL,
            style="italic")

    # ================================================================ mandate
    y_mandate = 11.0
    box(ax, XC - 2.7, y_mandate, 5.4, 0.70,
        title="ANALYST MANDATE — human-owned, immutable per case",
        edge=INK, face="#FFFFFF", title_size=8.0, lw=1.3)
    note(ax, XC - 3.65, y_mandate + 0.35,
         "company · information cutoff\n· report policy", size=6.2)
    note(ax, XC + 3.65, y_mandate + 0.35,
         "WACC · terminal growth ·\nforecast bounds · budgets", size=6.2)
    arrow(ax, XC, y_mandate, XC, 10.68)

    # ================================================================ orchestrator
    y_orch = 9.25
    box(ax, XC - 4.4, y_orch, 8.8, 1.43,
        title="ORCHESTRATOR",
        subtitle="pure Python, no LLM — coverageflow/orchestrator.py",
        lines=[
            "states: CREATED → RUNNING → PENDING_APPROVAL → APPROVED / REJECTED → CLOSED",
            "dispatch + state + routing · numerical gates · budgets · checkpoints · resume",
            "triggers: new dated doc · staleness check · human action",
            "backends per dispatch: claude / codex / kimi CLI · "
            "OpenAI-compatible API · offline replay",
        ],
        edge=NAVY, face="#E8EDF2", title_size=9.5, title_color=NAVY, lw=1.5,
        line_size=6.6)
    arrow(ax, XC, y_orch, XC, 8.93)

    # ================================================================ generator
    y_glead = 8.40
    box(ax, XC - 2.1, y_glead, 4.2, 0.53,
        title="GENERATOR LEAD — coordinates the build",
        edge=GEN_EDGE, face=GEN_FILL, title_size=7.9, lw=1.1)
    arrow(ax, XC, y_glead, XC, 8.10)

    y_spec, bh, bw, bgap = 6.80, 1.30, 2.42, 0.24
    total = 4 * bw + 3 * bgap
    x0 = XC - total / 2
    spec = [
        ("1 · DATA & INGEST",
         ["validate + classify doc;",
          "deterministic slicing;",
          "provenance manifest"]),
        ("2 · EXTRACTION",
         ["schema-bound facts;",
          "every number carries a",
          "citation to a document",
          "slice (only LLM step)"]),
        ("3 · MODEL & VALUATION",
         ["deterministic engines",
          "(pure Python) + specialist-",
          "proposed bounded forecast"]),
        ("4 · REPORT DRAFT",
         ["report.md strictly from",
          "the evidence ledger",
          "([EV: ...] links)"]),
    ]
    for i, (t, ls) in enumerate(spec):
        box(ax, x0 + i * (bw + bgap), y_spec, bw, bh, title=t, lines=ls,
            edge=GEN_EDGE, face=GEN_FILL, title_size=7.0, line_size=6.1)
    note(ax, XC, y_spec - 0.24,
         "candidate manifest = artifact IDs + SHA-256 · engines: "
         "model_engine@1.0 / valuation_engine@2.0", size=6.3)
    arrow(ax, XC, y_spec - 0.40, XC, 6.08)

    # ================================================================ gates
    y_gates = 5.30
    box(ax, XC - 3.6, y_gates, 7.2, 0.78,
        title="NUMERICAL GATES — deterministic, rerun before every review",
        lines=["cutoff · facts-schema · citations · forecast bounds · "
               "manifest-integrity · recompute · report evidence links",
               "gate fail → repair at the same role (no round consumed; "
               "2nd invalid → BLOCKED)"],
        edge=NAVY, face="#F5F7F8", title_size=7.6, line_size=6.0, lw=1.3)

    # ================================================================ critics
    y_clead = 4.42
    box(ax, XC - 2.1, y_clead, 4.2, 0.53,
        title="CRITIC LEAD — merges findings, keeps conflicts",
        edge=CRI_EDGE, face=CRI_FILL, title_size=7.7, lw=1.1)
    arrow(ax, XC, y_clead, XC, 4.19)

    y_crit, ch, cw, cgap = 2.94, 1.25, 2.42, 0.24
    totalc = 4 * cw + 3 * cgap
    cx0 = XC - totalc / 2
    crit = [
        ("C1 · EVIDENCE\n& CITATION",
         ["every cited number",
          "traces to a slice; no",
          "uncited number survives"]),
        ("C2 · ROBUSTNESS",
         ["independent recompute",
          "of model + valuation;",
          "forecast vs. history"]),
        ("C3 · TIMING\n& CUTOFF",
         ["nothing dated after",
          "the information cutoff",
          "leaks into outputs"]),
        ("C4 · RISK &\nCONTROLS",
         ["budgets, permissions,",
          "scope; carried findings",
          "verified or re-opened"]),
    ]
    for i, (t, ls) in enumerate(crit):
        box(ax, cx0 + i * (cw + cgap), y_crit, cw, ch,
            title=t, lines=ls, edge=CRI_EDGE, face=CRI_FILL,
            title_size=6.8, line_size=5.9)
    note(ax, XC, y_crit - 0.28,
         "4 fresh reviewers every round — fresh context per dispatch "
         "(stale request_id rejected)\n"
         "a finding closes ONLY on critic-verified repair "
         "(artifact + matching SHA-256)", size=6.3, ma="center")

    # ================================================================ repair loop
    x_loop = x0 + total + 0.42          # vertical loop lane (right margin)
    arrow(ax, x0 + total, y_crit + ch * 0.62, x_loop, y_crit + ch * 0.62,
          edge=CRI_EDGE, lw=1.0)
    arrow(ax, x_loop, y_crit + ch * 0.62, x_loop, y_spec + bh * 0.55,
          edge=CRI_EDGE, lw=1.0, alpha=0.9)
    arrow(ax, x_loop, y_spec + bh * 0.55, x0 + total, y_spec + bh * 0.55,
          edge=CRI_EDGE, lw=1.0)
    note(ax, x_loop + 0.22, (y_crit + y_spec) / 2 + 0.4,
         "REPAIR PACKET → full re-build (fresh eyes); any candidate change "
         "resets the clean streak",
         size=6.6, color=CRI_EDGE, rotation=90, ha="center")

    # acceptance note sits on the arrow into the human gate
    y_human = 1.36
    arrow(ax, XC, y_crit - 0.68, XC, y_human + 0.68, lw=1.1)
    note(ax, XC, y_crit - 0.68,
         "ACCEPTANCE: all gates pass · 2 consecutive clean full reviews of "
         "the same candidate\n"
         "· all carried findings closed by critic-verified repairs",
         size=6.4, weight="bold", ma="center")

    # ================================================================ human gate
    box(ax, XC - 2.7, y_human, 5.4, 0.68,
        title="HUMAN APPROVAL GATE — pause; auditable record; 48h deadline",
        subtitle="only the human approves; expiry → BLOCKED and "
                 "re-approval restarts acceptance",
        edge=HUM_EDGE, face="#FFFFFF", title_size=7.6, line_size=6.0, lw=1.4)
    note(ax, XC + 3.60, y_human + 0.34,
         "approve --decision APPROVED\n--by \"Gaetano Marco Chiantia\"",
         size=5.9)
    arrow(ax, XC, y_human, XC, 1.06)

    # ================================================================ published
    box(ax, XC - 3.1, 0.48, 6.2, 0.58,
        title="PUBLISHED REPORT VERSION  vA-<candidate8>",
        subtitle="report/ + export/ (model.xlsx · dossier.pdf · "
                 "dossier.html) — one verified evidence record",
        edge=INK, face="#FFFFFF", title_size=7.8, line_size=5.9, lw=1.2)

    # ================================================================ shared state band
    ax.add_patch(Rectangle((0.5, 0.02), W - 1.0, 0.42, facecolor="#F2F0EB",
                           edgecolor=FAINT, lw=0.8, zorder=1))
    ax.text(W / 2, 0.30, "SHARED STATE & EVIDENCE — JSON + JSONL, atomic "
            "writes (docs/contracts/state-and-artifacts.md)",
            ha="center", va="center", fontsize=7.2, family=SERIF,
            color=INK, fontweight="bold", zorder=3)
    ax.text(W / 2, 0.11,
            "state.json (only the Orchestrator writes) · trace.jsonl "
            "(append-only: REQUEST_BOUND → AGENT_DISPATCHED → GATE_RUN → "
            "FINDING_OPENED → ... → APPROVAL_RECORDED) · findings.json "
            "ledger · request IDs · candidate + artifact SHA-256 · "
            "checkpoints (history/) · versions/registry.json",
            ha="center", va="center", fontsize=5.9, family=SERIF,
            color=STEEL, zorder=3)

    # ================================================================ evolution panel (left)
    ex, ey, ew, eh = 0.5, 2.94, 2.95, 6.2
    ax.add_patch(FancyBboxPatch((ex, ey), ew, eh,
                                boxstyle="round,pad=0.02,rounding_size=0.06",
                                facecolor=EVO_FILL, edgecolor=EVO_EDGE, lw=1.1,
                                linestyle=(0, (5, 3)), zorder=2))
    ax.text(ex + ew / 2, ey + eh - 0.26, "EVOLUTION", ha="center", va="center",
            fontsize=8.4, family=SERIF, color=EVO_EDGE, fontweight="bold")
    ax.text(ex + ew / 2, ey + eh - 0.47, "version registry + human approval",
            ha="center", va="center", fontsize=6.2, family=SERIF,
            color=STEEL, style="italic")
    evo_lines = [
        "evolvable: prompt cards,",
        "mapping tables, engine code",
        "(regression must prove",
        "unchanged outputs on the",
        "frozen baseline cases),",
        "report templates",
        "",
        "locked (never evolvable):",
        "controller, gate thresholds,",
        "acceptance rules, rubric —",
        "a proposal cannot weaken",
        "its own acceptance standard",
        "",
        "loop: evo-prepare → scoped",
        "patch + tests → baseline",
        "regression (offline replay,",
        "never live tokens) → fresh-",
        "critic review → registry",
        "entry → human approve /",
        "defer / reject → active",
        "pointer swap",
        "",
        "rollback restores the prior",
        "pointer; existing cases keep",
        "their original version",
        "",
        "3-part evidence demo: pass",
        "→ approve · over-broad scope",
        "→ reject · rollback + verify",
    ]
    for i, ln in enumerate(evo_lines):
        ax.text(ex + ew / 2, ey + eh - 0.88 - i * 0.172, ln,
                ha="center", va="center", fontsize=6.0, family=SERIF,
                color=STEEL if ln else "none", zorder=3)
    arrow(ax, ex + ew / 2, ey, ex + ew / 2, 0.47, edge=EVO_EDGE, lw=0.9,
          ls=(0, (3, 2)), alpha=0.8)

    # ================================================================ legend (top-left)
    lx, ly = 0.55, 12.05
    ax.text(lx, ly, "LEGEND", fontsize=7.0, family=SERIF, color=INK,
            fontweight="bold", ha="left", va="center")
    legend = [
        ("deterministic code (harness)", NAVY, "#E8EDF2"),
        ("generator roles (LLM)", GEN_EDGE, GEN_FILL),
        ("critic roles (LLM, fresh)", CRI_EDGE, CRI_FILL),
        ("human gate", HUM_EDGE, "#FFFFFF"),
        ("evolution boundary", EVO_EDGE, EVO_FILL),
    ]
    for i, (t, eg, fg) in enumerate(legend):
        yy = ly - 0.34 - i * 0.30
        ax.add_patch(Rectangle((lx, yy - 0.085), 0.36, 0.17, edgecolor=eg,
                               facecolor=fg, lw=0.8, zorder=3))
        ax.text(lx + 0.48, yy, t, fontsize=6.4, family=SERIF, color=STEEL,
                va="center", ha="left")
    ax.text(lx, ly - 1.95,
            "Evidence runs (examples/) use the offline\n"
            "replay / stub dispatch with the real deterministic\n"
            "engines — every number below is produced by code,\n"
            "never guessed. Live backends swap in via config.",
            fontsize=6.1, family=SERIF, color=STEEL, va="top", ha="left")

    fig.subplots_adjust(left=0.005, right=0.995, top=0.995, bottom=0.005)
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, dpi=200, facecolor=PAPER)
    plt.close(fig)
    return out


def main() -> int:
    global SERIF
    SERIF = _serif()
    ap = argparse.ArgumentParser(description=__doc__)
    repo = Path(__file__).resolve().parent.parent
    ap.add_argument("--out", default=str(repo / "docs" / "workflow-diagram.png"))
    args = ap.parse_args()
    out = render(Path(args.out))
    kb = out.stat().st_size / 1024
    print(f"wrote {out} ({kb:.0f} KiB, serif={SERIF})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
