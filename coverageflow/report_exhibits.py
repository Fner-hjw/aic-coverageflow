"""Numbered exhibits (tables and the football-field data) for the research note.

Each builder reads only ``model.json`` / ``valuation.json`` values and returns
an exhibit dict::

    {"title", "unit", "source", "notes": [...], "width": "full" | "half",
     "table": {"header": [...], "align": ["l", "r", ...],
               "rows": [{"cells": [...], "style": "" | "total" | "sub", "box": [col]}]}}

A cell with no value is ``"n.a."``.  Exhibit ids ("Exhibit 3") are assigned by
the caller in body order.  Formatting helpers come from ``report_doc``.
"""

from __future__ import annotations

import re
from typing import Any

NA = "n.a."


# --------------------------------------------------------------- helpers
def _fmt():
    from . import report_doc as rd  # late import: report_doc imports this module
    return rd


def _s(model: dict, field: str) -> dict:
    return (model.get("series") or {}).get(field) or {}


def _v(model: dict, field: str, period: str | None) -> float | None:
    x = _s(model, field).get(period) if period else None
    return None if x is None else float(x)


def _yoy_or_calc(model: dict, field: str, cur: str | None, prior: str | None) -> float | None:
    yd = _s(model, field).get("yoy")
    y = yd.get(cur) if isinstance(yd, dict) else None
    if y is not None:
        return float(y)
    a, b = _v(model, field, cur), _v(model, field, prior)
    if a is None or b is None or b <= 0:
        return None
    return a / b - 1


def _pp(a: float | None, b: float | None) -> str:
    if a is None or b is None:
        return NA
    d = (a - b) * 100
    return f"{'+' if d >= 0 else _fmt().MINUS}{abs(d):.1f} pp"


def _mult(x: Any) -> str:
    return NA if x is None else f"{float(x):.1f}x"


def _tbl(header: list[str], rows: list[dict], align: list[str] | None = None) -> dict:
    return {"header": header, "rows": rows,
            "align": align or ["l"] + ["r"] * (len(header) - 1)}


def _row(cells: list[str], style: str = "", box: list[int] | None = None) -> dict:
    r: dict = {"cells": cells, "style": style}
    if box:
        r["box"] = box
    return r


# ------------------------------------------------------------ exhibits
def income_exhibit(model: dict, q: str, pq: str, ytd: str, pytd: str, q_label: str,
                   pq_label: str, ytd_label: str, pytd_label: str) -> dict | None:
    rd = _fmt()
    rev = _v(model, "income_statement.revenue", q)
    if rev is None:
        return None
    has_ytd = _v(model, "income_statement.revenue", ytd) is not None

    def line(label, field, kind="mm", style=""):
        cells = [label]
        for cur, pri in (((q, pq),) + (((ytd, pytd),) if has_ytd else ())):
            a, b = _v(model, field, cur), _v(model, field, pri)
            if kind == "mm":
                cells += [rd.f_mm(a) if a is not None else NA, rd.f_mm(b) if b is not None else NA,
                          rd.f_pct(_yoy_or_calc(model, field, cur, pri), 1, True)
                          if _yoy_or_calc(model, field, cur, pri) is not None else NA]
            elif kind == "eps":
                cells += [rd.f_usd(a) if a is not None else NA, rd.f_usd(b) if b is not None else NA,
                          rd.f_pct(_yoy_or_calc(model, field, cur, pri), 1, True)
                          if _yoy_or_calc(model, field, cur, pri) is not None else NA]
            else:  # margin
                cells += [rd.f_pct(a) if a is not None else NA, rd.f_pct(b) if b is not None else NA,
                          _pp(a, b)]
        return _row(cells, style)

    header = ["", q_label, pq_label, "y/y"] + ([ytd_label, pytd_label, "y/y"] if has_ytd else [])
    rows = [line("Revenue", "income_statement.revenue"),
            line("Gross profit", "income_statement.gross_profit"),
            line("Gross margin", "derived.gross_margin", "pct", "sub"),
            line("Operating expenses", "income_statement.operating_expenses"),
            line("Operating income", "income_statement.operating_income"),
            line("Operating margin", "derived.operating_margin", "pct", "sub"),
            line("Net income", "income_statement.net_income"),
            line("Diluted EPS (USD)", "income_statement.diluted_eps", "eps")]
    rows = [r for r in rows if any(c != NA for c in r["cells"][1:])]
    return {"title": f"Income statement summary, {q_label} vs {pq_label}", "unit": "USD mm except per share",
            "source": f"Source: company filings (SEC), CoverageFlow model engine ({model.get('engine', '')}).",
            "notes": [], "width": "full", "table": _tbl(header, rows)}


def segment_exhibit(model: dict, q: str, pq: str, ytd: str, q_label: str, ytd_label: str) -> dict | None:
    rd = _fmt()
    slugs = list(model.get("segment_slugs") or [])
    if not slugs:
        return None
    rows = []
    for slug in slugs:
        name = rd._seg_name(slug)
        rev, oi = _v(model, f"segments.{slug}.revenue", q), _v(model, f"segments.{slug}.operating_income", q)
        if rev is None and oi is None:
            continue
        yoy = _yoy_or_calc(model, f"segments.{slug}.revenue", q, pq)
        share = _v(model, f"segments.{slug}.revenue_share_of_total", q)
        om = _v(model, f"segments.{slug}.operating_margin", q)
        rev9, oi9 = _v(model, f"segments.{slug}.revenue", ytd), _v(model, f"segments.{slug}.operating_income", ytd)
        rows.append(_row([name, rd.f_mm(rev) if rev is not None else NA,
                          rd.f_pct(yoy, 1, True) if yoy is not None else NA,
                          rd.f_pct(share) if share is not None else NA,
                          rd.f_mm(oi) if oi is not None else NA,
                          rd.f_pct(om) if om is not None else NA,
                          rd.f_mm(rev9) if rev9 is not None else NA,
                          rd.f_mm(oi9) if oi9 is not None else NA]))
    if not rows:
        return None
    ao, ao9 = _v(model, "segments.all_other_operating_income", q), _v(model, "segments.all_other_operating_income", ytd)
    if ao is not None:
        rows.append(_row(["All other", NA, NA, NA, rd.f_mm(ao), NA, NA, rd.f_mm(ao9) if ao9 is not None else NA]))
    trev, toi = _v(model, "income_statement.revenue", q), _v(model, "income_statement.operating_income", q)
    if trev is not None:
        rows.append(_row(["Group", rd.f_mm(trev),
                          rd.f_pct(_yoy_or_calc(model, "income_statement.revenue", q, pq), 1, True),
                          rd.f_pct(1.0), rd.f_mm(toi) if toi is not None else NA,
                          rd.f_pct(_v(model, "derived.operating_margin", q)),
                          rd.f_mm(_v(model, "income_statement.revenue", ytd)),
                          rd.f_mm(_v(model, "income_statement.operating_income", ytd))], "total"))
    has9 = any(r["cells"][6] != NA or r["cells"][7] != NA for r in rows)
    header = ["", f"Revenue {q_label}", "y/y", "% of group", "Op. income", "Op. margin",
              f"Revenue {ytd_label}", f"Op. income {ytd_label}"]
    t = _tbl(header, rows)
    if not has9:
        t["header"] = header[:6]
        for r in t["rows"]:
            r["cells"] = r["cells"][:6]
        t["align"] = t["align"][:6]
    return {"title": f"Segment results, {q_label}", "unit": "USD mm",
            "source": f"Source: company filings (SEC, segment note), CoverageFlow model engine ({model.get('engine', '')}).",
            "notes": [], "width": "full", "table": t}


def dcf_exhibit(model: dict, val: dict, base_fy: int) -> dict | None:
    rd = _fmt()
    dcf = val.get("dcf") or {}
    years = dcf.get("years") or []
    if not years:
        return None
    base = dcf.get("base") or {}
    base_lab = f"FY{base_fy}A" if base_fy else "Base"
    header = ["", base_lab] + [f"FY{base_fy + i + 1}E" if base_fy else f"Year {i + 1}" for i in range(len(years))]

    def line(label, bkey, ykey, fmt, style="", base_val=None):
        b = base.get(bkey) if bkey else base_val
        cells = [label, fmt(b) if b is not None else NA]
        for y in years:
            v = y.get(ykey)
            cells.append(fmt(v) if v is not None else NA)
        return _row(cells, style)

    pct = lambda v: rd.f_pct(v, 1)
    rows = [line("Revenue", "revenue", "revenue", rd.f_mm),
            line("Growth y/y", None, "growth", lambda v: rd.f_pct(v, 1, True),
                 base_val=_yoy_or_calc(model, "income_statement.revenue", f"FY{base_fy}", f"FY{base_fy - 1}")),
            line("EBITDA", "ebitda", "ebitda", rd.f_mm),
            line("EBITDA margin", "ebitda_margin", "ebitda_margin", pct, "sub"),
            line("EBIT", "ebit", "ebit", rd.f_mm),
            line("Capital expenditure", "capex", "capex", rd.f_mm),
            line("Free cash flow", "fcf", "fcf", rd.f_mm, "total"),
            line("Discount factor", None, "discount_factor", lambda v: f"{v:.4f}"),
            line("PV of free cash flow", None, "pv", rd.f_mm, "total")]
    notes = []
    if dcf.get("discount_convention"):
        notes.append(f"Discounting: {str(dcf['discount_convention']).replace('_', ' ')} "
                     f"at WACC {rd.f_pct(dcf.get('wacc'), 1)}; valuation date {(dcf.get('discounting') or {}).get('valuation_date', 'n.a.')}.")
    reason = (dcf.get("base") or {}).get("reason")
    if reason:
        notes.append("Base year: fiscal-year fallback (trailing-twelve-month base could not be built).")
    return {"title": "DCF summary", "unit": "USD mm", "notes": notes, "width": "full",
            "source": f"Source: CoverageFlow valuation engine ({val.get('engine', '')}), dcf.years.",
            "table": _tbl(header, rows)}


def bridge_exhibit(val: dict) -> dict | None:
    rd = _fmt()
    dcf = val.get("dcf") or {}
    if not dcf.get("ev"):
        return None
    term, ev, eq, ps = dcf.get("terminal") or {}, dcf.get("ev") or {}, dcf.get("equity_value") or {}, dcf.get("per_diluted_share") or {}
    br = dcf.get("bridge") or {}
    nd = br.get("net_debt", dcf.get("net_debt"))

    def two(label, fn, style=""):
        return _row([label] + [fn(k) for k in ("gordon", "exit_multiple")], style)

    def g(d, k, fmt):
        v = d.get(k) if isinstance(d, dict) else None
        return fmt(v) if v is not None else NA

    nci = br.get("nci")
    nci_txt = NA if br.get("nci_source") == "absent" or nci is None else rd.f_mm(nci)
    rows = [two("PV of free cash flow", lambda k: rd.f_mm(dcf.get("pv_sum"))),
            two("Terminal value", lambda k: g(term.get(k) or {}, "terminal_value", rd.f_mm)),
            two("PV of terminal value", lambda k: g(term.get(k) or {}, "pv", rd.f_mm)),
            two("Enterprise value", lambda k: g(ev, k, rd.f_mm), "total"),
            two("Less: net debt" + (" (negative = net cash)" if (nd or 0) < 0 else ""),
                lambda k: rd.f_mm(nd) if nd is not None else NA),
            two("Less: non-controlling interests", lambda k: nci_txt),
            two("Equity value", lambda k: g(eq, k, rd.f_mm), "total"),
            two("Diluted shares (mm)", lambda k: rd.f_mm(dcf.get("diluted_shares"))),
            two("Value per share (USD)", lambda k: g(ps, k, rd.f_usd), "total"),
            _row(["Implied terminal EV/EBITDA", _mult(term.get("implied_exit_multiple_from_gordon", dcf.get("implied_exit_multiple_from_gordon"))),
                  _mult((term.get("exit_multiple") or {}).get("multiple"))], "sub"),
            _row(["Implied terminal growth", rd.f_pct(((term.get("gordon") or {}).get("terminal_growth")), 1),
                  rd.f_pct(term.get("implied_growth_from_exit", dcf.get("implied_growth_from_exit")), 1)], "sub")]
    return {"title": "From enterprise value to value per share", "unit": "USD mm", "width": "half",
            "notes": [], "source": f"Source: CoverageFlow valuation engine ({val.get('engine', '')}), dcf.ev / bridge / terminal.",
            "table": _tbl(["", "Gordon growth", "Exit multiple"], rows)}


def sensitivity_exhibit(val: dict) -> dict | None:
    rd = _fmt()
    sens = val.get("sensitivity") or {}
    grid, wp, gp = sens.get("grid"), sens.get("wacc_points"), sens.get("terminal_growth_points")
    if not (grid and wp and gp):
        return None
    dcf = val.get("dcf") or {}
    bw, bg = dcf.get("wacc"), (dcf.get("terminal", {}).get("gordon") or {}).get("terminal_growth")
    bi = min(range(len(wp)), key=lambda i: abs(wp[i] - bw)) if bw is not None else None
    bj = min(range(len(gp)), key=lambda j: abs(gp[j] - bg)) if bg is not None else None
    rows = []
    for i, w in enumerate(wp):
        cells = [rd.f_pct(w, 1)] + [rd.f_usd(x) for x in grid[i]]
        rows.append(_row(cells, "", [bj + 1] if (i == bi and bj is not None) else None))
    return {"title": "Gordon DCF value per share by WACC and terminal growth", "unit": "USD per share",
            "width": "half", "notes": ["Rows: WACC; columns: terminal growth. Boxed cell is the base case."],
            "source": f"Source: CoverageFlow valuation engine ({val.get('engine', '')}), sensitivity.grid.",
            "table": _tbl(["WACC / growth"] + [rd.f_pct(g, 1) for g in gp], rows)}


def peers_exhibit(val: dict) -> dict | None:
    rd = _fmt()
    comps = val.get("comps") or {}
    peers = comps.get("peers") or []
    if not peers:
        return None
    ap = comps.get("applied") or {}
    rows = [_row([p.get("ticker", NA), _mult(p.get("ev_ebitda")), _mult(p.get("p_e_ttm")), _mult(p.get("p_s")),
                  rd.f_pct(p.get("rev_growth_yoy"), 1, True) if p.get("rev_growth_yoy") is not None else NA])
            for p in peers]
    rows.append(_row(["Median", _mult((ap.get("ev_ebitda") or {}).get("peer_median")), NA,
                      _mult((ap.get("p_s") or {}).get("peer_median")), NA], "total"))
    return {"title": "Peer multiples", "unit": f"as of {rd.f_date(comps.get('as_of'))}", "width": "half", "notes": [],
            "source": f"Source: SEC companyfacts and closing prices (peer block), CoverageFlow valuation engine ({val.get('engine', '')}).",
            "table": _tbl(["", "EV/EBITDA", "P/E (TTM)", "P/S", "Rev. growth"], rows)}


def implied_exhibit(val: dict, ticker: str, base_fy: int) -> dict | None:
    rd = _fmt()
    comps = val.get("comps") or {}
    ap = comps.get("applied") or {}
    if not ap:
        return None
    e, s = ap.get("ev_ebitda") or {}, ap.get("p_s") or {}
    fy = f"FY{base_fy}" if base_fy else "base year"
    rows = [_row([f"EV/EBITDA on {fy} EBITDA", rd.f_mm(e.get("own_metric")) if e else NA, _mult(e.get("peer_median")),
                  rd.f_usd(e.get("implied_price")) if e.get("implied_price") is not None else NA]),
            _row([f"P/S on {fy} revenue", rd.f_mm(s.get("own_metric")) if s else NA, _mult(s.get("peer_median")),
                  rd.f_usd(s.get("implied_price")) if s.get("implied_price") is not None else NA])]
    if comps.get("comps_price") is not None:
        rows.append(_row(["Comps-implied value (average)", "", "", rd.f_usd(comps.get("comps_price"))], "total"))
    notes = []
    if comps.get("comps_upside") is not None:
        notes.append(f"Comps-implied value versus price: {rd.f_pct(comps.get('comps_upside'), 1, True)}.")
    if val.get("cross_check_note"):
        notes.append(str(val["cross_check_note"]))
    return {"title": f"Relative valuation of {ticker}", "unit": "USD mm; per share in USD", "width": "half", "notes": notes,
            "source": f"Source: CoverageFlow valuation engine ({val.get('engine', '')}), comps.applied.",
            "table": _tbl(["", f"{ticker} metric", "Peer median", "Per share"], rows)}


def football_data(val: dict) -> dict | None:
    """Rows for the football-field chart: low/high/point per method (USD per share)."""
    rd = _fmt()
    dcf, comps, sens = val.get("dcf") or {}, val.get("comps") or {}, val.get("sensitivity") or {}
    rows: list[dict] = []
    grid, wp, gp = sens.get("grid"), sens.get("wacc_points"), sens.get("terminal_growth_points")
    ps = dcf.get("per_diluted_share") or {}
    if grid and wp and gp and dcf.get("wacc") is not None:
        bg = (dcf.get("terminal", {}).get("gordon") or {}).get("terminal_growth")
        bi = min(range(len(wp)), key=lambda i: abs(wp[i] - dcf["wacc"]))
        bj = min(range(len(gp)), key=lambda j: abs(gp[j] - bg)) if bg is not None else 0
        ii = [i for i in (bi - 1, bi, bi + 1) if 0 <= i < len(wp)]
        jj = [j for j in (bj - 1, bj, bj + 1) if 0 <= j < len(gp)]
        vals = [grid[i][j] for i in ii for j in jj]
        rows.append({"label": f"DCF, Gordon (WACC {wp[ii[0]] * 100:.0f}{_fmt().EN}{wp[ii[-1]] * 100:.0f}%, "
                              f"g {gp[jj[0]] * 100:.1f}{_fmt().EN}{gp[jj[-1]] * 100:.1f}%)",
                     "low": min(vals), "high": max(vals), "point": ps.get("gordon")})
    elif ps.get("gordon") is not None:
        rows.append({"label": "DCF, Gordon growth", "low": None, "high": None, "point": ps["gordon"]})
    if ps.get("exit_multiple") is not None:
        mult = ((dcf.get("terminal") or {}).get("exit_multiple") or {}).get("multiple")
        rows.append({"label": "DCF, exit multiple" + (f" ({mult:.1f}x)" if mult else ""),
                     "low": None, "high": None, "point": ps["exit_multiple"]})
    ap, band = comps.get("applied") or {}, comps.get("own_band") or {}
    for key, name in (("ev_ebitda", "Comps, EV/EBITDA (peer range)"), ("p_s", "Comps, P/S (peer range)")):
        b = (band.get(key) or {}).get("implied_price")
        pt = (ap.get(key) or {}).get("implied_price")
        if b and len(b) == 2:
            rows.append({"label": name, "low": b[0], "high": b[1], "point": pt})
    if not rows:
        return None
    return {"rows": rows, "price": dcf.get("market_price"),
            "price_as_of": rd.f_date(dcf.get("market_price_as_of")),
            "title": "Valuation range, USD per share", "unit": "USD per share",
            "notes": ["Bars show ranges, diamonds the point estimate (median multiple, base-case DCF); "
                      "the vertical line is the share price."],
            "source": f"Source: CoverageFlow valuation engine ({val.get('engine', '')}), dcf, sensitivity, comps."}


# --------------------------------------------------------------- daggers
_NUM = re.compile(r"\d[\d,]*(?:\.\d+)?")


def exception_tokens(exceptions: list[dict]) -> set[str]:
    """Numbers (comma-less, at least 4 characters) quoted in open-finding summaries."""
    toks: set[str] = set()
    for e in exceptions:
        for m in _NUM.findall(str(e.get("summary", ""))):
            t = m.replace(",", "")
            if len(t) >= 4 and not (len(t) == 4 and t.isdigit() and 1990 <= int(t) <= 2100):
                toks.add(t)
    return toks


def mark_daggers(table: dict, toks: set[str]) -> bool:
    """Append a dagger to body cells quoting a number from an open finding."""
    hit = False
    for r in table["rows"]:
        for i, c in enumerate(r["cells"][1:], start=1):
            if any(m.replace(",", "") in toks for m in _NUM.findall(c)):
                r["cells"][i] = c + "†"
                hit = True
    return hit
