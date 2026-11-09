"""TDD smoke + style tests for export_xlsx.

Builds the model on the same valuation fixture (hand-known numbers: FY2025
revenue 52,000; 2026-Q3 revenue 13,500; DCF per share exit-multiple 9.5495)
and exports the 5-tab institutional workbook.
"""

from pathlib import Path

import pytest
import openpyxl

from coverageflow.model_engine import Facts, build_model
from coverageflow.valuation_engine import Assumptions, Forecast, value_company
from coverageflow.export_xlsx import export_model_xlsx

from .test_valuation import ASSUMPTIONS, FORECAST, valuation_payload

TABS = ["Statements Model", "Quarterly Financials", "DCF", "Company Comparison", "Ratios"]


@pytest.fixture
def workbook(tmp_path) -> tuple[Path, openpyxl.Workbook]:
    model = build_model(Facts.from_payload(valuation_payload()), "1.0")
    valuation = value_company(model, Forecast.from_payload(FORECAST),
                               Assumptions.from_payload(ASSUMPTIONS))
    out = export_model_xlsx(model, valuation, tmp_path / "model.xlsx")
    return out, openpyxl.load_workbook(out)


def _cells(ws):
    for row in ws.iter_rows(values_only=False):
        for c in row:
            if c.value is not None:
                yield c


def _find(ws, value, rel=1e-6):
    hits = [c.coordinate for c in _cells(ws)
            if isinstance(c.value, (int, float)) and abs(c.value - value) <= abs(value) * rel]
    return hits


def test_export_returns_path_and_tabs(tmp_path, workbook):
    out, wb = workbook
    assert isinstance(out, Path)
    assert out.is_file()
    assert wb.sheetnames == TABS


def test_no_formulas_no_gridlines(workbook):
    _, wb = workbook
    for ws in wb.worksheets:
        assert ws.sheet_view.showGridLines is False
        for c in _cells(ws):
            assert not (isinstance(c.value, str) and c.value.startswith("=")), \
                f"formula found at {ws.title}!{c.coordinate}"


def test_statements_model_fy_columns_and_values(workbook):
    ws = workbook[1]["Statements Model"]
    flat = " | ".join(str(c.value) for row in ws.iter_rows(min_row=1, max_row=8, max_col=8)
                      for c in row if c.value is not None)
    assert "FY2025" in flat
    # FY2025 revenue 52,000 and gross margin 35.0% exist as static values
    rev_cells = _find(ws, 52000.0)
    assert rev_cells, "FY2025 revenue 52,000 not found"
    gm_cells = _find(ws, 0.35, rel=1e-9)
    assert gm_cells, "gross margin 0.35 not found"
    # one of the revenue cells carries the thousands-separator format
    assert any(ws[c].number_format == "#,##0.0" for c in rev_cells)
    assert any(ws[c].number_format == "0.0%" for c in gm_cells)


def test_quarterly_financials_has_quarters(workbook):
    ws = workbook[1]["Quarterly Financials"]
    flat = " | ".join(str(c.value) for c in _cells(ws) if not isinstance(c.value, float))
    assert "2026-Q3" in flat and "2025-Q3" in flat
    assert _find(ws, 13500.0), "quarterly revenue 13,500 missing"
    assert _find(ws, 12500.0), "prior-year revenue 12,500 missing"


def test_dcf_tab_has_projection_and_market_vs_intrinsic(workbook):
    ws = workbook[1]["DCF"]
    flat = "\n".join(str(c.value) for c in _cells(ws) if isinstance(c.value, str))
    for label in ("WACC", "Terminal growth", "Exit multiple", "Revenue",
                  "PV", "Terminal value", "Enterprise value", "Equity value",
                  "per diluted share", "Market vs. intrinsic"):
        assert label.lower() in flat.lower(), f"DCF tab missing label: {label}"
    # hand-computed DCF outputs present (exit per-share 9.5495, Gordon 6.8439)
    assert _find(ws, 9.5495, rel=1e-3), "exit-multiple per-share value missing"
    assert _find(ws, 6.8439, rel=1e-3), "Gordon per-share value missing"
    assert _find(ws, 12603.515, rel=1e-3), "PV sum missing"


def test_company_comparison_has_peer_block(workbook):
    ws = workbook[1]["Company Comparison"]
    text = [str(c.value) for c in _cells(ws) if not isinstance(c.value, (int, float))]
    flat = " | ".join(text)
    assert "PEER1" in flat
    assert "EV/EBITDA" in flat or "EV / EBITDA" in flat
    assert _find(workbook[1]["Company Comparison"], 20.0), "peer P/E 20x missing"
    assert _find(workbook[1]["Company Comparison"], 4.75428571, rel=1e-4) or \
        _find(workbook[1]["Company Comparison"], 12.0), "peer multiple values missing"


def test_ratios_tab_profitability(workbook):
    ws = workbook[1]["Ratios"]
    flat = "\n".join(str(c.value) for c in _cells(ws) if isinstance(c.value, str))
    for label in ("Gross Margin", "Operating Margin", "Net Margin", "ROE"):
        assert label.lower() in flat.lower(), f"Ratios tab missing: {label}"
    # FY2025: ROA = 1,040/140,000 = 0.0074286 ; ROE = 1,040/64,000 = 0.01625
    assert _find(ws, 1040.0 / 66000.0, rel=1e-6) or _find(ws, 1040.0 / 64000.0, rel=1e-6), \
        "ROE row missing"


def test_export_is_deterministic_in_values(tmp_path):
    model = build_model(Facts.from_payload(valuation_payload()), "1.0")
    val = value_company(model, Forecast.from_payload(FORECAST),
                        Assumptions.from_payload(ASSUMPTIONS))
    p1 = export_model_xlsx(model, val, tmp_path / "a.xlsx")
    p2 = export_model_xlsx(model, val, tmp_path / "b.xlsx")
    wb1, wb2 = openpyxl.load_workbook(p1), openpyxl.load_workbook(p2)
    for name in TABS:
        for r1, r2 in zip(wb1[name].iter_rows(values_only=True),
                          wb2[name].iter_rows(values_only=True)):
            assert r1 == r2
