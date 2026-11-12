"""Restrained charts for the research note (matplotlib; vector output).

``segment_chart(chart)`` / ``football_chart(fb)`` take the chart dicts of a
``report_doc`` and return ``(svg_text, pdf_bytes)``; ``make_charts(doc)``
builds all of them and returns ``{}`` when matplotlib is unavailable, so the
note renders without charts instead of failing.  Desk green (UI --accent) for the current period, grey for the
comparison period, no gridlines, no frame -- the figure title and source line
are set by the renderers, not baked into the image.
"""

from __future__ import annotations

import io
from pathlib import Path

try:  # charts are optional: callers fall back to no figure
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt  # noqa: E402
    from matplotlib import font_manager  # noqa: E402
except Exception:  # pragma: no cover - exercised by a monkeypatched test
    matplotlib = None  # type: ignore[assignment]

ACCENT, GREY, INK, MUTE = "#1A8A4D", "#B9C2C9", "#1C2733", "#51606F"
_FONT_DIRS = [Path("/usr/local/texlive/2026/texmf-dist/fonts/opentype/adobe/sourcesans")]


def _font() -> str:
    for d in _FONT_DIRS:
        for f in ("SourceSans3-Regular.otf", "SourceSans3-Semibold.otf"):
            if (d / f).exists():
                font_manager.fontManager.addfont(str(d / f))
    names = {f.name for f in font_manager.fontManager.ttflist}
    return "Source Sans 3" if "Source Sans 3" in names else "DejaVu Sans"


def segment_chart(chart: dict, width_mm: float = 103, height_mm: float = 50) -> tuple[str, bytes]:
    fam = _font()
    plt.rcParams.update({"font.family": fam, "svg.fonttype": "none", "pdf.fonttype": 42})
    cats, series = chart["categories"], chart["series"]
    fig, ax = plt.subplots(figsize=(width_mm / 25.4, height_mm / 25.4))
    fig.patch.set_alpha(0)
    ax.patch.set_alpha(0)
    n = len(series)
    w = 0.34
    colors = [GREY, ACCENT][-n:]
    top = max(v for s in series for v in s["values"]) * 1.18
    for si, s in enumerate(series):
        xs = [i + (si - (n - 1) / 2) * (w + 0.04) for i in range(len(cats))]
        ax.bar(xs, s["values"], w, color=colors[si], linewidth=0)
        for x, v in zip(xs, s["values"]):
            ax.text(x, v + top * 0.02, f"{v:,.0f}", ha="center", va="bottom", fontsize=7.2,
                    color=INK if si == n - 1 else MUTE)
    ax.set_xticks(range(len(cats)))
    ax.set_xticklabels([f"{c['name']}\n{c['note']}" for c in cats], fontsize=7.4, color=INK,
                       linespacing=1.25)
    ax.tick_params(axis="x", length=0, pad=4)
    ax.set_yticks([])
    ax.set_ylim(0, top)
    for sp in ("top", "right", "left"):
        ax.spines[sp].set_visible(False)
    ax.spines["bottom"].set_color("#B9BEC4")
    ax.spines["bottom"].set_linewidth(0.5)
    handles = [plt.Rectangle((0, 0), 1, 1, color=colors[i], linewidth=0) for i in range(n)]
    ax.legend(handles, [s["label"] for s in series], loc="upper right", frameon=False, fontsize=7.2,
              handlelength=0.9, handleheight=0.9, labelcolor=MUTE, borderaxespad=0, bbox_to_anchor=(1.0, 1.0))
    fig.subplots_adjust(left=0.01, right=0.9, top=0.97, bottom=0.2)
    svg, pdf = io.StringIO(), io.BytesIO()
    fig.savefig(svg, format="svg", transparent=True)
    fig.savefig(pdf, format="pdf", transparent=True)
    plt.close(fig)
    return svg.getvalue(), pdf.getvalue()


def football_chart(fb: dict, width_mm: float = 174, height_mm: float = 42) -> tuple[str, bytes]:
    """Horizontal range bars (USD per share) with the current price as a vertical line."""
    fam = _font()
    plt.rcParams.update({"font.family": fam, "svg.fonttype": "none", "pdf.fonttype": 42})
    rows = fb["rows"]
    fig, ax = plt.subplots(figsize=(width_mm / 25.4, height_mm / 25.4))
    fig.patch.set_alpha(0)
    ax.patch.set_alpha(0)
    price = fb.get("price")
    xmax = max([r["high"] if r.get("high") is not None else r["point"] for r in rows]
               + ([price] if price else [])) * 1.12
    n = len(rows)
    for i, r in enumerate(rows):
        y = n - 1 - i
        lo, hi, pt = r.get("low"), r.get("high"), r.get("point")
        if lo is not None and hi is not None:
            ax.barh(y, hi - lo, left=lo, height=0.42, color=ACCENT, linewidth=0)
            ax.text(lo - xmax * 0.008, y, f"{lo:,.0f}", ha="right", va="center", fontsize=7.2, color=MUTE)
            ax.text(hi + xmax * 0.008, y, f"{hi:,.0f}", ha="left", va="center", fontsize=7.2, color=MUTE)
        if pt is not None:
            ax.plot([pt], [y], marker="D", markersize=4.6, color=INK, markeredgewidth=0, zorder=3)
            if lo is None:
                ax.text(pt + xmax * 0.012, y, f"{pt:,.2f}", ha="left", va="center", fontsize=7.2, color=INK)
    if price:
        ax.axvline(price, color=INK, linewidth=0.8, ymin=0, ymax=1)
        ax.text(price - xmax * 0.006, n - 0.45, f"Price {price:,.2f}", ha="right", va="bottom",
                fontsize=7.2, color=INK)
    ax.set_yticks(range(n))
    ax.set_yticklabels([r["label"] for r in reversed(rows)], fontsize=7.4, color=INK)
    ax.tick_params(axis="y", length=0, pad=4)
    ax.tick_params(axis="x", labelsize=7, colors=MUTE, length=2, width=0.4)
    ax.set_xlim(0, xmax)
    ax.set_ylim(-0.6, n - 0.3)
    for sp in ("top", "right", "left"):
        ax.spines[sp].set_visible(False)
    ax.spines["bottom"].set_color("#CFD8DF")
    ax.spines["bottom"].set_linewidth(0.5)
    fig.subplots_adjust(left=0.37, right=0.975, top=0.9, bottom=0.14)
    svg, pdf = io.StringIO(), io.BytesIO()
    fig.savefig(svg, format="svg", transparent=True)
    fig.savefig(pdf, format="pdf", transparent=True)
    plt.close(fig)
    return svg.getvalue(), pdf.getvalue()


def make_charts(doc: dict) -> dict[str, tuple[str, bytes]]:
    """{'segments': (svg, pdf), 'football': (svg, pdf)} for the charts the doc carries."""
    out: dict[str, tuple[str, bytes]] = {}
    if matplotlib is None:
        return out
    try:
        if doc.get("chart"):
            out["segments"] = segment_chart(doc["chart"])
        fb = doc.get("football")
        if fb and fb.get("rows"):
            out["football"] = football_chart(fb)
    except Exception:  # never let a chart failure block the note
        return out
    return out
