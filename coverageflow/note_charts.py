"""Restrained charts for the research note (matplotlib; vector output).

``segment_chart(chart)`` takes ``report_doc["chart"]`` and returns
``(svg_text, pdf_bytes)``.  Navy for the current period, grey for the
comparison period, no gridlines, no frame -- the figure title and source line
are set by the renderers, not baked into the image.
"""

from __future__ import annotations

import io
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib import font_manager  # noqa: E402

NAVY, GREY, INK, MUTE = "#1F3A5F", "#B3B9C0", "#1B1B1B", "#5E6670"
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
    plt.rcParams.update({"font.family": fam, "svg.fonttype": "path", "pdf.fonttype": 42})
    cats, series = chart["categories"], chart["series"]
    fig, ax = plt.subplots(figsize=(width_mm / 25.4, height_mm / 25.4))
    n = len(series)
    w = 0.34
    colors = [GREY, NAVY][-n:]
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
              handlelength=0.9, handleheight=0.9, labelcolor=MUTE, borderaxespad=0)
    fig.subplots_adjust(left=0.01, right=0.99, top=0.97, bottom=0.2)
    svg, pdf = io.StringIO(), io.BytesIO()
    fig.savefig(svg, format="svg")
    fig.savefig(pdf, format="pdf")
    plt.close(fig)
    return svg.getvalue(), pdf.getvalue()
