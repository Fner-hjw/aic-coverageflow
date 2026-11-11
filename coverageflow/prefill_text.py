"""Human formatting for the mandate prefill text (basis sentences, captions).

One place for percentages, multiples, money and dates so the CLI review table,
the stored provenance and the UI captions read the same. Text only: numeric
values in provenance are never changed.
"""

from __future__ import annotations

from datetime import date


def _trim(s: str) -> str:
    return s.rstrip("0").rstrip(".") if "." in s else s


def pct(x: float, nd: int = 2) -> str:
    """0.049 -> '4.90%' (nd decimals)."""
    return f"{x * 100:.{nd}f}%"


def pct_short(x: float, nd: int = 1) -> str:
    """Percent with trailing zeros trimmed: 0.21 -> '21%', 0.136 -> '13.6%'."""
    return f"{_trim(f'{x * 100:.{nd}f}')}%"


def mult(x: float, nd: int = 1) -> str:
    """24.18 -> '24.2x'."""
    return f"{x:.{nd}f}x"


def usd(v: float) -> str:
    """USD in thousands/millions/billions, never scientific: 1.03e8 -> 'USD 103m'."""
    sign = "-" if v < 0 else ""
    a = abs(float(v))
    for div, unit in ((1e12, "tn"), (1e9, "bn"), (1e6, "m")):
        if a >= div:
            n = a / div
            txt = f"{n:.0f}" if n >= 100 else _trim(f"{n:.1f}")
            return f"{sign}USD {txt}{unit}"
    return f"{sign}USD {a:,.0f}"


def day(text: str | None) -> str:
    """'2026-11-04' -> '4 Nov 2026' (the input's first ten characters)."""
    if not text:
        return ""
    d = date.fromisoformat(str(text)[:10])
    return f"{d.day} {d:%b} {d.year}"


def year(text: str | None) -> str:
    return str(text or "")[:4]


def points(x: float) -> str:
    """0.05 -> '5 points' (percentage points)."""
    n = x * 100
    s = _trim(f"{n:.1f}")
    return f"{s} point" + ("" if s == "1" else "s")


def number_list(items, fmt=mult) -> str:
    return ", ".join(fmt(v) for v in items)


SOURCE_LABELS = {
    "yahoo": "Yahoo Finance close",
    "stooq": "Stooq close",
    "treasury:par_yield_10y": "US Treasury",
    "fred:DGS10": "FRED",
    "derived": "Computed",
    "default": "Default",
    "sec:company_tickers": "SEC ticker index",
    "sec:companyfacts": "SEC filings",
    "references/peer-sets.json": "Curated peer list",
    "mandate": "Mandate",
    "user": "Entered by you",
    "none": "Not available",
}


def source_label(source: str | None) -> str:
    s = source or ""
    if s.startswith("sec:sic-"):
        return "SEC same-industry list"
    return SOURCE_LABELS.get(s, s.replace("_", " ").capitalize())


def caption(entry: dict) -> str:
    """'<Source label>, <date>. <Basis>' as the UI shows it."""
    label = source_label(entry.get("source"))
    when = day(entry.get("as_of")) if entry.get("as_of") else ""
    return f"{label}{', ' + when if when else ''}. {entry.get('basis') or ''}".strip()
