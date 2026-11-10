"""Institutional xlsx export of the model + valuation artifacts.

Owner stream: A1 (runtime & data).

Contract (docs/contracts/interfaces.md section 4)::

    def export_model_xlsx(model, valuation, out: Path) -> Path

Produces an openpyxl workbook with the five tabs set by the design record:
"Statements Model" (FY columns across the top), "Quarterly Financials",
"DCF" (projections + PV + terminal + market-vs-intrinsic block),
"Company Comparison" (dated peer block), "Ratios" - following the layout
conventions of the supplied reference workbooks: absolute line items as
rows with ratio rows directly beneath their absolutes, clean number
formats (thousands separators, one decimal on percentages), no gridlines,
light header fills only, static values (no formulas, no external links,
no add-ins).
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

if TYPE_CHECKING:  # pragma: no cover
    from coverageflow.model_engine import Model
    from coverageflow.valuation_engine import Valuation

ENGINE_NAME = "export_xlsx@1.0"

# ------- style constants (clean institutional, per reference templates)
NAVY = "1F3864"
SECTION_FILL = PatternFill("solid", fgColor="D9E2F3")   # light blue section band
HEADER_FILL = PatternFill("solid", fgColor="EDF2F9")   # pale column header
THIN_BOTTOM = Border(bottom=Side(style="thin", color="8EA9DB"))
FMT_ABS = "#,##0.0"
FMT_SHARES = "#,##0"
FMT_PRICE = "0.00"
FMT_PCT = "0.0%"
FMT_MULT = '0.0"x"'
FMT_FACTOR = "0.000"


def _default_schema_path() -> Path:
    return Path(__file__).resolve().parent.parent / "references" / "model-schema.json"


def _load_labels() -> dict[str, str]:
    try:
        schema = json.loads(_default_schema_path().read_text("utf-8"))
    except OSError:
        return {}
    labels: dict[str, str] = {}
    for stmt in schema.get("statements", {}).values():
        for f in stmt.get("fields", []):
            labels[f["id"]] = f["label"]
    return labels


# ------------------------------------------------------------- series utils

_SERIES_META = ("yoy", "qoq", "yoy_computed", "qoq_computed")


def _values(model: Any, fld: str) -> dict[str, float]:
    series = model.series if hasattr(model, "series") else model.get("series", {})
    entry = series.get(fld, {})
    # bool is an int in Python, so the *_computed flags would otherwise land
    # in the sheet as 0.0 columns
    return {k: float(v) for k, v in entry.items()
            if k not in _SERIES_META and isinstance(v, (int, float))
            and not isinstance(v, bool)}


def _period_key(p: str) -> tuple[int, int, int]:
    """Sort key for a period label: FY2025, 2026-Q3, 2026-Q3-YTD.

    A 10-Q reports the quarter *and* the year-to-date span, and the model
    keeps them as separate series keys, so both have to order.
    """
    if p.startswith("FY"):
        return int(p[2:]), 0, 0
    year, rest = p.split("-Q", 1)
    q, _, span = rest.partition("-")
    return int(year), int(q), 1 if span.upper() == "YTD" else 0


def _periods(model: Any, kind: str) -> list[str]:
    """kind 'FY' -> fiscal-year labels ('FY2025'); 'Q' -> quarter labels ('2026-Q3')."""
    keys: set[str] = set()
    for fld_series in (model.series if hasattr(model, "series")
                       else model.get("series", {})).values():
        for k in fld_series:
            if not isinstance(k, str) or k in _SERIES_META:
                continue
            if kind == "FY" and k.startswith("FY"):
                keys.add(k)
            elif kind == "Q" and "-Q" in k:
                keys.add(k)
    # a label the exporter cannot order is not a column it can render; skip it
    # rather than failing the whole workbook
    ordered = []
    for k in keys:
        try:
            _period_key(k)
        except (ValueError, IndexError):
            continue
        ordered.append(k)
    return sorted(ordered, key=_period_key)


def _val(d: dict, *path) -> Any:
    cur: Any = d
    for p in path:
        if cur is None:
            return None
        cur = cur.get(p) if isinstance(cur, dict) else None
    return cur


# ------------------------------------------------------------- sheet helpers

def _sheet(wb: Workbook, title: str, first: bool = False) -> Any:
    ws = wb.active if first else wb.create_sheet()
    ws.title = title
    ws.sheet_view.showGridLines = False
    ws.column_dimensions["A"].width = 38
    return ws


def _title(ws, text: str, meta: str | None = None) -> None:
    ws["A1"] = text
    ws["A1"].font = Font(bold=True, size=13, color=NAVY)
    row = 2
    if meta:
        ws["A2"] = meta
        ws["A2"].font = Font(size=9, color="595959")
        row = 3
    ws.freeze_panes = "A4"
    return row + 1


def _section(ws, row: int, text: str, width: int) -> int:
    c = ws.cell(row=row, column=1, value=text)
    c.font = Font(bold=True, color=NAVY)
    for col in range(1, width + 1):
        ws.cell(row=row, column=col).fill = SECTION_FILL
    return row + 1


def _header_row(ws, row: int, labels: list[str]) -> int:
    for i, label in enumerate(labels, start=1):
        c = ws.cell(row=row, column=i, value=label)
        c.font = Font(bold=True)
        c.fill = HEADER_FILL
        c.border = THIN_BOTTOM
        c.alignment = Alignment(horizontal="center" if i > 1 else "left")
        if i > 1:
            ws.column_dimensions[get_column_letter(i)].width = max(11, min(14, len(label) + 2))
    return row + 1


def _data_rows(ws, row: int, periods: list[str],
               rows: list[tuple[str, dict[str, float] | None, str, float | None]]) -> int:
    """rows: (label, period->value map, number format, optional ratio expected in [0,1])."""
    for label, series, fmt, _ in rows:
        if series is None:
            continue
        if not any(p in series for p in periods):
            continue
        ws.cell(row=row, column=1, value=label).font = Font()
        for i, p in enumerate(periods, start=2):
            v = series.get(p)
            if v is not None:
                cell = ws.cell(row=row, column=i, value=round(float(v), 6))
                cell.number_format = fmt
        row += 1
    return row


_STATEMENTS = (
    ("income_statement.", "Income statement"),
    ("balance_sheet.", "Balance sheet"),
    ("cash_flow.", "Cash flow"),
    ("segments.", "Segment detail"),
)


def _has_data(series: dict[str, float] | None, periods: list[str]) -> bool:
    return bool(series) and any(p in series for p in periods)


def _section_rows(ws, row: int, title: str, width: int, periods: list[str],
                  rows: list[tuple[str, dict[str, float] | None, str, float | None]]) -> int:
    """A section header only when at least one of its rows has data.

    Printing "Income Statement" over nothing is what made the workbook look
    half-built: the header promised a statement the case never extracted.
    """
    live = [r for r in rows if _has_data(r[1], periods)]
    if not live:
        return row
    row = _section(ws, row, title, width)
    return _data_rows(ws, row, periods, live)


def _coverage(model: Any) -> list[tuple[str, list[str]]]:
    """Which periods each statement actually carries."""
    series = model.series if hasattr(model, "series") else model.get("series", {})
    out = []
    for prefix, label in _STATEMENTS:
        periods: set[str] = set()
        for fld, entry in series.items():
            if not fld.startswith(prefix) or not isinstance(entry, dict):
                continue
            for k in entry:
                if not isinstance(k, str) or k in _SERIES_META:
                    continue
                try:
                    _period_key(k)
                except (ValueError, IndexError):
                    continue
                periods.add(k)
        out.append((label, sorted(periods, key=_period_key)))
    return out


def _coverage_block(ws, row: int, model: Any, width: int = 6) -> int:
    """State what the workbook contains, and what it does not.

    A reader opening an empty FY column deserves to know whether the figure is
    zero, missing, or simply never extracted.
    """
    row = _section(ws, row, "Data coverage", width)
    for label, periods in _coverage(model):
        ws.cell(row=row, column=1, value=label)
        c = ws.cell(row=row, column=2,
                    value=", ".join(periods) if periods else "not extracted")
        c.font = Font(size=9, color="595959")
        row += 1
    annual = any(p.startswith("FY") for label, ps in _coverage(model)
                 for p in ps if label == "Income statement")
    if not annual:
        c = ws.cell(row=row, column=1, value=(
            "Annual income-statement and cash-flow columns are empty because "
            "no annual filing was extracted for this case; a case triggered on "
            "a 10-Q carries quarterly and year-to-date figures only."))
        c.font = Font(size=9, italic=True, color="595959")
        row += 1
    return row + 1


def _kv_row(ws, row: int, label: str, value: Any, fmt: str | None = None,
            indent: bool = False) -> int:
    c = ws.cell(row=row, column=1, value=("    " + label) if indent else label)
    if value is not None:
        vcell = ws.cell(row=row, column=2, value=value if isinstance(value, (int, float, str)) else None)
        if fmt and isinstance(value, (int, float)):
            vcell.number_format = fmt
    return row + 1


# ------------------------------------------------------------ the exporter

def export_model_xlsx(model: "Model | dict", valuation: "Valuation | dict",
                      out: Path, company: str = "Covered Company") -> Path:
    """Write the 5-tab workbook to out (atomic), return its Path."""
    vdict = valuation.to_dict() if hasattr(valuation, "to_dict") else dict(valuation or {})
    dcf = vdict.get("dcf", {})
    comps = vdict.get("comps", {})
    assumptions = vdict.get("assumptions", {})
    labels = _load_labels()
    L = lambda fid: labels.get(fid, fid)  # noqa: E731

    fy = _periods(model, "FY")
    qs = _periods(model, "Q")
    meta = None
    cand = getattr(model, "candidate_id", None) or vdict.get("candidate_id")
    cutoff = vdict.get("cutoff")
    if cand or cutoff:
        meta = " / ".join(x for x in (f"candidate {cand}" if cand else None,
                                     f"cutoff {cutoff}" if cutoff else None) if x)

    wb = Workbook()
    wb.properties.created = datetime(2020, 1, 1)
    wb.properties.modified = datetime(2020, 1, 1)

    _build_statements(wb, model, L, company, meta, fy)
    _build_quarterly(wb, model, L, company, meta, qs)
    _build_dcf(wb, dcf, vdict, company, meta)
    _build_comparison(wb, comps, model, L, company, meta, fy)
    _build_ratios(wb, model, L, company, meta, fy, qs)

    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(str(out) + ".tmp")
    wb.save(str(tmp))
    os.replace(tmp, out)
    return out


# ------------------------------------------------------------------- tabs

def _is_rows(model, L) -> list[tuple[str, dict[str, float] | None, str, float | None]]:
    return [
        (L("income_statement.revenue"), _values(model, "income_statement.revenue"), FMT_ABS, None),
        (L("income_statement.cost_of_revenue"), _values(model, "income_statement.cost_of_revenue"), FMT_ABS, None),
        (L("income_statement.gross_profit"), _values(model, "income_statement.gross_profit"), FMT_ABS, None),
        ("Gross Margin", _values(model, "derived.gross_margin"), FMT_PCT, 1.0),
        (L("income_statement.research_and_development"), _values(model, "income_statement.research_and_development"), FMT_ABS, None),
        (L("income_statement.selling_general_and_administrative"), _values(model, "income_statement.selling_general_and_administrative"), FMT_ABS, None),
        (L("income_statement.operating_expenses"), _values(model, "income_statement.operating_expenses"), FMT_ABS, None),
        (L("income_statement.depreciation_and_amortization"), _values(model, "income_statement.depreciation_and_amortization"), FMT_ABS, None),
        (L("income_statement.ebitda"), _values(model, "income_statement.ebitda"), FMT_ABS, None),
        (L("income_statement.operating_income"), _values(model, "income_statement.operating_income"), FMT_ABS, None),
        ("Operating Margin", _values(model, "derived.operating_margin"), FMT_PCT, 1.0),
        (L("income_statement.income_before_tax"), _values(model, "income_statement.income_before_tax"), FMT_ABS, None),
        (L("income_statement.income_tax_expense"), _values(model, "income_statement.income_tax_expense"), FMT_ABS, None),
        (L("income_statement.net_income"), _values(model, "income_statement.net_income"), FMT_ABS, None),
        ("Net Margin", _values(model, "derived.net_margin"), FMT_PCT, 1.0),
        (L("income_statement.diluted_eps"), _values(model, "income_statement.diluted_eps"), FMT_PRICE, None),
        (L("income_statement.diluted_shares_outstanding"), _values(model, "income_statement.diluted_shares_outstanding"), FMT_SHARES, None),
    ]


def _bs_rows(model, L):
    rows = []
    for fid in ("cash_and_equivalents", "total_current_assets", "property_plant_equipment_net",
                "goodwill", "intangible_assets_net", "total_assets", "accounts_payable",
                "short_term_debt", "total_current_liabilities", "long_term_debt",
                "total_liabilities", "total_stockholders_equity"):
        rows.append((L(f"balance_sheet.{fid}"), _values(model, f"balance_sheet.{fid}"), FMT_ABS, None))
    rows.append(("Net Debt", _values(model, "derived.net_debt"), FMT_ABS, None))
    return rows


def _cf_rows(model, L):
    rows = [
        (L("cash_flow.net_income"), _values(model, "cash_flow.net_income"), FMT_ABS, None),
        (L("cash_flow.depreciation_amortization"), _values(model, "cash_flow.depreciation_amortization"), FMT_ABS, None),
        (L("cash_flow.stock_based_compensation"), _values(model, "cash_flow.stock_based_compensation"), FMT_ABS, None),
        (L("cash_flow.operating_cash_flow"), _values(model, "cash_flow.operating_cash_flow"), FMT_ABS, None),
        (L("cash_flow.capital_expenditure"), _values(model, "cash_flow.capital_expenditure"), FMT_ABS, None),
        ("Free Cash Flow", _values(model, "derived.fcf"), FMT_ABS, None),
    ]
    for fid in ("dividend_paid", "share_issuance_repurchase", "debt_issuance_repayment",
                "financing_cash_flow", "cash_at_end_of_period"):
        rows.append((L(f"cash_flow.{fid}"), _values(model, f"cash_flow.{fid}"), FMT_ABS, None))
    return rows


def _segment_rows(model, measure: str) -> list[tuple[str, dict[str, float] | None, str, float | None]]:
    segs = ("CCG", "DCAI", "NEX", "Foundry", "Altera", "Mobileye")
    rows = []
    for seg in segs:
        rows.append((f"{seg} Revenue", _values(model, f"segments.{seg.lower()}.revenue"), FMT_ABS, None))
        if measure == "margin":
            rows.append((f"{seg} Operating Margin",
                         _values(model, f"segments.{seg.lower()}.operating_margin"), FMT_PCT, 1.0))
    return rows


def _build_statements(wb, model, L, company, meta, fy):
    ws = _sheet(wb, "Statements Model", first=True)
    periods = fy or _periods(model, "Q")
    row = _title(ws, f"{company}: Statements Model",
                 meta or "annual series (USD millions unless noted)")
    row = _coverage_block(ws, row, model, len(periods) + 1)
    row = _header_row(ws, row, ["FY"] + periods)
    row = _section_rows(ws, row, "Income Statement", len(periods) + 1,
                        periods, _is_rows(model, L))
    row += 1
    row = _section_rows(ws, row, "Balance Sheet", len(periods) + 1,
                        periods, _bs_rows(model, L))
    row += 1
    row = _section_rows(ws, row, "Cash Flow Statement", len(periods) + 1,
                        periods, _cf_rows(model, L))
    row += 1
    row = _section_rows(ws, row, "Segments (consolidated)", len(periods) + 1,
                        periods, _segment_rows(model, "margin"))
    return ws


def _build_quarterly(wb, model, L, company, meta, qs):
    ws = _sheet(wb, "Quarterly Financials")
    periods = qs or _periods(model, "FY")
    row = _title(ws, f"{company}: Quarterly Financials",
                 meta or "quarterly series (USD millions unless noted)")
    row = _header_row(ws, row, ["Qtr"] + periods)
    rows = _is_rows(model, L)
    rows += [
        ("Revenue Growth YoY", _growth(model, "income_statement.revenue", "yoy"), FMT_PCT, None),
        ("Revenue Growth QoQ", _growth(model, "income_statement.revenue", "qoq"), FMT_PCT, None),
    ]
    row = _section_rows(ws, row, "Income Statement", len(periods) + 1,
                        periods, rows)
    row += 1
    row = _section_rows(ws, row, "Cash Flow", len(periods) + 1, periods, [
        (L("cash_flow.operating_cash_flow"), _values(model, "cash_flow.operating_cash_flow"), FMT_ABS, None),
        (L("cash_flow.capital_expenditure"), _values(model, "cash_flow.capital_expenditure"), FMT_ABS, None),
        ("Free Cash Flow", _values(model, "derived.fcf"), FMT_ABS, None),
    ])
    row += 1
    segs = ("CCG", "DCAI", "NEX", "Foundry", "Altera", "Mobileye")
    seg_rows = [(f"{s} Revenue", _values(model, f"segments.{s.lower()}.revenue"), FMT_ABS, None)
                for s in segs]
    row = _section_rows(ws, row, "Segments (consolidated revenue)",
                        len(periods) + 1, periods, seg_rows)


def _growth(model, fld, kind):
    series = model.series if hasattr(model, "series") else model.get("series", {})
    entry = series.get(fld, {})
    g = entry.get(kind) or {}
    return {k: float(v) for k, v in g.items()} if isinstance(g, dict) else {}


def _build_dcf(wb, dcf, vdict, company, meta):
    ws = _sheet(wb, "DCF")
    row = _title(ws, f"{company}: Discounted Cash Flow",
                 meta or "5-year projection, terminal by exit multiple (primary) and Gordon growth (cross-check)")
    assump = vdict.get("assumptions", {})
    note = vdict.get("cross_check_note", "")
    row = _section(ws, row, "Assumptions (mandate)", 8)
    row = _kv_row(ws, row, "WACC", _val(dcf, "wacc"), FMT_PCT)
    row = _kv_row(ws, row, "Terminal growth", _val(assump, "terminal_growth"), FMT_PCT)
    row = _kv_row(ws, row, "Exit multiple (EBITDA)", _val(assump, "exit_multiple_ebitda"), FMT_MULT)
    row = _kv_row(ws, row, "Discount convention", _val(dcf, "discount_convention"))
    row += 1

    # net debt and share count are read off the model, not the mandate, and
    # they come from the latest balance sheet, which may be a quarter later
    # than the FY column a reader is comparing against
    row = _section(ws, row, "Bridge inputs (latest balance sheet)", 8)
    nd_period = _val(dcf, "net_debt_period")
    sh_period = _val(dcf, "shares_period")
    row = _kv_row(ws, row, f"Net debt ({nd_period})" if nd_period else "Net debt",
                  _val(dcf, "net_debt"), FMT_ABS)
    row = _kv_row(ws, row, f"Diluted shares, mm ({sh_period})" if sh_period
                  else "Diluted shares (mm)", _val(dcf, "diluted_shares"), FMT_SHARES)
    if nd_period:
        c = ws.cell(row=row, column=1, value=(
            f"Net debt is taken at {nd_period}, the latest balance sheet in the "
            "case. The Statements Model reports it per fiscal year, so the two "
            "tabs agree only where the latest period is that fiscal year."))
        c.font = Font(size=9, italic=True, color="595959")
        row += 1
    row += 1

    years = dcf.get("years", [])
    n = len(years)
    row = _section(ws, row, "Cash Flow Projection", n + 2)
    row = _header_row(ws, row, ["USD millions"] + [f"Y{y['year']}" for y in years])
    labels = [("revenue", "Revenue", FMT_ABS),
              ("gross_margin", "Gross Margin", FMT_PCT),
              ("fcf_margin", "FCF Margin", FMT_PCT),
              ("ebitda_margin", "EBITDA Margin", FMT_PCT),
              ("ebitda", "EBITDA", FMT_ABS),
              ("fcf", "Free Cash Flow", FMT_ABS),
              ("discount_factor", "Discount Factor", FMT_FACTOR),
              ("pv", "PV of FCF", FMT_ABS)]
    for key, label, fmt in labels:
        if key == "ebitda" and years and "ebitda" not in years[0]:
            has = False
        else:
            has = any(key in y for y in years)
        if not has:
            continue
        ws.cell(row=row, column=1, value=label)
        for i, y in enumerate(years, start=2):
            v = y.get(key)
            if v is not None:
                c = ws.cell(row=row, column=i, value=round(float(v), 6))
                c.number_format = fmt
        row += 1
    row = _kv_row(ws, row, "Sum of PV of FCF", _val(dcf, "pv_sum"), FMT_ABS)
    row += 1

    row = _section(ws, row, "Terminal Value", 8)
    row = _kv_row(ws, row, "Exit multiple method", None)
    row = _kv_row(ws, row, "Final EBITDA", _val(dcf, "terminal", "exit_multiple", "ebitda_final"), FMT_ABS, indent=True)
    row = _kv_row(ws, row, "Terminal value", _val(dcf, "terminal", "exit_multiple", "terminal_value"), FMT_ABS, indent=True)
    row = _kv_row(ws, row, "PV of terminal value", _val(dcf, "terminal", "exit_multiple", "pv"), FMT_ABS, indent=True)
    row = _kv_row(ws, row, "Gordon growth method (cross-check)", None)
    row = _kv_row(ws, row, "Terminal value", _val(dcf, "terminal", "gordon", "terminal_value"), FMT_ABS, indent=True)
    row = _kv_row(ws, row, "PV of terminal value", _val(dcf, "terminal", "gordon", "pv"), FMT_ABS, indent=True)
    row += 1

    row = _section(ws, row, "Valuation Bridge", 8)
    row = _kv_row(ws, row, "Enterprise value - exit multiple", _val(dcf, "ev", "exit_multiple"), FMT_ABS)
    row = _kv_row(ws, row, "Enterprise value - Gordon", _val(dcf, "ev", "gordon"), FMT_ABS)
    row = _kv_row(ws, row, "Equity value - exit multiple", _val(dcf, "equity_value", "exit_multiple"), FMT_ABS)
    row = _kv_row(ws, row, "Equity value - Gordon", _val(dcf, "equity_value", "gordon"), FMT_ABS)
    row = _kv_row(ws, row, "Value per diluted share - exit multiple", _val(dcf, "per_diluted_share", "exit_multiple"), FMT_PRICE)
    row = _kv_row(ws, row, "Value per diluted share - Gordon", _val(dcf, "per_diluted_share", "gordon"), FMT_PRICE)
    # the two terminal methods are a cross-check: when they disagree by more
    # than 3x the projection, not the terminal value, is doing the work, and a
    # reader who averages them would be averaging a number with no support
    em = _val(dcf, "per_diluted_share", "exit_multiple")
    gg = _val(dcf, "per_diluted_share", "gordon")
    if isinstance(em, (int, float)) and isinstance(gg, (int, float)) and em and gg:
        ratio = max(abs(em), abs(gg)) / max(min(abs(em), abs(gg)), 1e-9)
        if ratio > 3:
            c = ws.cell(row=row, column=1, value=(
                f"Methods disagree {ratio:.0f}x. The projected free cash flow is "
                "negative or near zero, so the Gordon terminal value collapses "
                "while the exit multiple still capitalises EBITDA. Neither "
                "figure is a valuation until the FCF path is credible."))
            c.font = Font(size=9, italic=True, color="C00000")
            row += 1
    row += 1

    row = _section(ws, row, "Market vs. intrinsic", 8)
    dcf_upside = _val(dcf, "dcf_upside")
    comps_price = _val(vdict, "comps", "comps_price")
    comps_upside = _val(vdict, "comps", "comps_upside")
    delta = _val(vdict, "cross_check_delta")
    within = _val(vdict, "within_tolerance")
    # a label with no number beside it reads as a broken workbook; print only
    # the comparisons that could actually be computed
    market_rows = [
        ("DCF upside vs market", dcf_upside, FMT_PCT),
        ("Comps implied price", comps_price, FMT_PRICE),
        ("Comps upside vs market", comps_upside, FMT_PCT),
        ("Cross-check delta (DCF - comps)", delta, FMT_PCT),
    ]
    printed = 0
    for label, value, fmt in market_rows:
        if value is None:
            continue
        row = _kv_row(ws, row, label, value, fmt)
        printed += 1
    # the tolerance verdict is always stated: "unavailable" is information,
    # a missing row is not
    row = _kv_row(ws, row, "Within mandate tolerance",
                  "unavailable" if within is None else ("yes" if within else "no"))
    if not printed:
        c = ws.cell(row=row, column=1,
                    value="No market comparison: the mandate sets no market "
                          "price, so no upside can be computed.")
        c.font = Font(size=9, italic=True, color="595959")
        row += 1
    if note:
        c = ws.cell(row=row, column=1, value="Cross-check note: " + note)
        c.font = Font(size=9, color="595959")
        row += 1
        # let long notes span visually via generous column A
    return ws


def _build_comparison(wb, comps, model, L, company, meta, fy):
    ws = _sheet(wb, "Company Comparison")
    row = _title(ws, f"{company}: Company Comparison",
                 meta or f"dated peer block (as of {comps.get('as_of') or 'n/a'})")
    peers = comps.get("peers", [])
    applied = comps.get("applied", {})

    rows = [
        ("EV/EBITDA", [None] + [p.get("ev_ebitda") for p in peers], FMT_MULT),
        ("P/E (TTM)", [None] + [p.get("p_e_ttm") for p in peers], FMT_MULT),
        ("P/S", [None] + [p.get("p_s") for p in peers], FMT_MULT),
        ("Revenue growth (YoY)", [None] + [p.get("rev_growth_yoy") for p in peers], FMT_PCT),
    ]
    latest_fy = fy[-1] if fy else None
    rows.append(("Revenue (FY)", [(_values(model, "income_statement.revenue").get(latest_fy)
                                  if latest_fy else None)] + [None] * len(peers), FMT_ABS))
    rows.append(("Net income (FY)", [(_values(model, "income_statement.net_income").get(latest_fy)
                                      if latest_fy else None)] + [None] * len(peers), FMT_ABS))
    rows.append(("Diluted EPS (FY)", [(_values(model, "income_statement.diluted_eps").get(latest_fy)
                                       if latest_fy else None)] + [None] * len(peers), FMT_PRICE))
    rows = [r for r in rows if not all(v is None for v in r[1])]
    if rows:
        row = _header_row(ws, row, ["Metric"] + [company]
                          + [str(p.get("ticker", "?")) for p in peers])
    for label, vals, fmt in rows:
        ws.cell(row=row, column=1, value=label)
        for i, v in enumerate(vals, start=2):
            if v is not None:
                c = ws.cell(row=row, column=i, value=round(float(v), 6))
                c.number_format = fmt
        row += 1
    row += 1
    implied = [(label, _val(applied.get(method), "implied_price"))
               for method, label in (("ev_ebitda", "From EV/EBITDA"),
                                     ("p_e", "From P/E"),
                                     ("p_s", "From P/S"))]
    implied = [(label, v) for label, v in implied if v is not None]
    if implied:
        row = _section(ws, row,
                       "Implied value per share (median peer multiple, own metrics)",
                       2 + len(peers))
        for label, v in implied:
            row = _kv_row(ws, row, label, v, FMT_PRICE)
        row = _kv_row(ws, row, "Comps price (median of methods)",
                      comps.get("comps_price"), FMT_PRICE)
        row = _kv_row(ws, row, "Comps upside vs market",
                      comps.get("comps_upside"), FMT_PCT)
    else:
        c = ws.cell(row=row, column=1,
                    value="No peer multiples: the mandate carries no dated "
                          "peer block, so no implied price can be computed.")
        c.font = Font(size=9, italic=True, color="595959")
        row += 2
    band = comps.get("own_band", {})
    if band:
        # a negative implied price is not a valuation, it is a loss-making
        # denominator: printing it invites the reader to average it in
        usable = [(k, band[k]) for k in ("ev_ebitda", "p_e", "p_s")
                  if band.get(k) and min(band[k]["implied_price"]) > 0]
        dropped = [k for k in ("ev_ebitda", "p_e", "p_s")
                   if band.get(k) and min(band[k]["implied_price"]) <= 0]
        row += 1
        row = _section(ws, row, "Own historical multiple band", 3)
        if usable:
            row = _header_row(ws, row, ["Multiple", "Low", "High"])
            for key, b in usable:
                ws.cell(row=row, column=1,
                        value=f"{key} band [{float(b['band'][0]):.1f}x, "
                              f"{float(b['band'][1]):.1f}x]")
                for i, v in enumerate(b["implied_price"], start=2):
                    c = ws.cell(row=row, column=i, value=round(float(v), 6))
                    c.number_format = FMT_PRICE
                row += 1
        if dropped:
            c = ws.cell(row=row, column=1, value=(
                f"{', '.join(dropped)} omitted: the covered company's "
                "denominator is negative, so the implied price is negative "
                "and carries no valuation meaning."))
            c.font = Font(size=9, italic=True, color="595959")
            row += 1
    return ws


def _build_ratios(wb, model, L, company, meta, fy, qs):
    ws = _sheet(wb, "Ratios")
    row = _title(ws, f"{company}: Ratios", meta or "profitability and growth analysis")
    rows = [
        ("Gross Margin", _values(model, "derived.gross_margin"), FMT_PCT),
        ("Operating Margin", _values(model, "derived.operating_margin"), FMT_PCT),
        ("EBITDA Margin", _values(model, "derived.ebitda_margin"), FMT_PCT),
        ("Net Margin", _values(model, "derived.net_margin"), FMT_PCT),
    ]
    ni = _values(model, "income_statement.net_income")
    ta = _values(model, "balance_sheet.total_assets")
    eq = _values(model, "balance_sheet.total_stockholders_equity")
    rev = _values(model, "income_statement.revenue")
    fcf = _values(model, "derived.fcf")
    ratios: list[tuple[str, dict[str, float], str]] = rows + [
        ("ROA", {p: ni[p] / ta[p] for p in ni if p in ta and ta[p]}, FMT_PCT),
        ("ROE", {p: ni[p] / eq[p] for p in ni if p in eq and eq[p]}, FMT_PCT),
        ("FCF Margin", {p: fcf[p] / rev[p] for p in fcf if p in rev and rev[p]}, FMT_PCT),
        ("Revenue Growth YoY", _growth(model, "income_statement.revenue", "yoy"), FMT_PCT),
        ("Net Income Growth YoY", _growth(model, "income_statement.net_income", "yoy"), FMT_PCT),
        ("Diluted EPS Growth YoY", _growth(model, "income_statement.diluted_eps", "yoy"), FMT_PCT),
    ]
    # a ratio exists only where its inputs do: a case with no annual income
    # statement has no annual margins, so fall back to the columns that carry
    # data instead of printing an empty table under an FY header
    available: set[str] = set()
    for _, series, _ in ratios:
        available |= set(series)
    periods = ([p for p in (fy or []) if p in available]
               or [p for p in (qs or []) if p in available]
               or sorted(available, key=_period_key))
    if not periods:
        c = ws.cell(row=row, column=1,
                    value="No ratios: the case carries no income-statement "
                          "series to compute them from.")
        c.font = Font(size=9, italic=True, color="595959")
        return ws
    row = _header_row(ws, row, [""] + periods)
    for label, series, fmt in ratios:
        if not _has_data(series, periods):
            continue
        ws.cell(row=row, column=1, value=label)
        for i, p in enumerate(periods, start=2):
            v = series.get(p)
            if v is not None:
                c = ws.cell(row=row, column=i, value=round(float(v), 6))
                c.number_format = fmt
        row += 1
    return ws
