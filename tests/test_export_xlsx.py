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


def test_dcf_tab_displays_unavailable_cross_check(tmp_path):
    model = build_model(Facts.from_payload(valuation_payload()), "1.0")
    assumptions = Assumptions.from_payload({**ASSUMPTIONS, "peer_block": {"peers": []}})
    valuation = value_company(model, Forecast.from_payload(FORECAST), assumptions)
    path = export_model_xlsx(model, valuation, tmp_path / "unavailable.xlsx")
    ws = openpyxl.load_workbook(path)["DCF"]
    label = next(c for c in _cells(ws) if c.value == "Within mandate tolerance")
    assert ws.cell(label.row, label.column + 1).value == "unavailable"


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


class TestYearToDateSpans:
    """A 10-Q reports the quarter and the year-to-date span as separate keys.

    The model keeps both (`2026-Q3` and `2026-Q3-YTD`), so the exporter has to
    order both. It used to do `int("3-YTD")` and take the whole workbook down
    with a ValueError, which surfaced in the UI as a 500 on "Excel model".
    """

    def _model(self):
        payload = valuation_payload()
        for fact in payload["facts"]:
            if fact["field"] == "income_statement.revenue":
                fact.setdefault("periods", {})
        model = build_model(Facts.from_payload(payload), "1.0")
        model.series["income_statement.revenue"]["2026-Q3-YTD"] = 46218.0
        model.series["income_statement.revenue"]["2025-Q3-YTD"] = 39179.0
        return model

    def test_period_key_orders_the_ytd_span_after_its_quarter(self):
        from coverageflow.export_xlsx import _period_key
        assert _period_key("2026-Q3") < _period_key("2026-Q3-YTD")
        assert _period_key("2025-Q3-YTD") < _period_key("2026-Q3")
        assert _period_key("FY2025") < _period_key("2026-Q3")

    def test_quarterly_tab_carries_both_spans(self, tmp_path):
        model = self._model()
        valuation = value_company(model, Forecast.from_payload(FORECAST),
                                  Assumptions.from_payload(ASSUMPTIONS))
        out = export_model_xlsx(model, valuation, tmp_path / "model.xlsx")
        ws = openpyxl.load_workbook(out)["Quarterly Financials"]
        header = [c.value for c in ws[4]]
        assert "2026-Q3" in header and "2026-Q3-YTD" in header
        assert header.index("2026-Q3") < header.index("2026-Q3-YTD")

    def test_an_unorderable_period_label_is_skipped_not_fatal(self, tmp_path):
        model = self._model()
        model.series["income_statement.revenue"]["not-a-period"] = 1.0
        valuation = value_company(model, Forecast.from_payload(FORECAST),
                                  Assumptions.from_payload(ASSUMPTIONS))
        out = export_model_xlsx(model, valuation, tmp_path / "model.xlsx")
        ws = openpyxl.load_workbook(out)["Quarterly Financials"]
        assert "not-a-period" not in [c.value for c in ws[4]]

    def test_computed_flags_do_not_become_zero_columns(self, tmp_path):
        model = self._model()
        model.series["income_statement.revenue"]["qoq_computed"] = False
        valuation = value_company(model, Forecast.from_payload(FORECAST),
                                  Assumptions.from_payload(ASSUMPTIONS))
        out = export_model_xlsx(model, valuation, tmp_path / "model.xlsx")
        ws = openpyxl.load_workbook(out)["Quarterly Financials"]
        assert "qoq_computed" not in [c.value for c in ws[4]]


class TestCoverageNoteTellsTheTruth:
    """The coverage block must describe the workbook it is printed into.

    It used to test `label.startswith("income_statement")` against a label that
    reads "Income statement", so the "no annual filing was extracted" sentence
    printed on every workbook, including ones carrying three FY columns.
    """

    def _export(self, tmp_path, model):
        valuation = value_company(model, Forecast.from_payload(FORECAST),
                                  Assumptions.from_payload(ASSUMPTIONS))
        out = export_model_xlsx(model, valuation, tmp_path / "model.xlsx")
        return openpyxl.load_workbook(out)["Statements Model"]

    def _text(self, ws):
        return " ".join(str(c.value) for c in _cells(ws))

    def test_annual_columns_suppress_the_missing_annual_note(self, tmp_path):
        model = build_model(Facts.from_payload(valuation_payload()), "1.0")
        assert any(p.startswith("FY") for p in model.series["income_statement.revenue"])
        ws = self._export(tmp_path, model)
        assert "no annual filing was extracted" not in self._text(ws)

    def test_quarterly_only_case_still_explains_the_empty_annual_columns(self, tmp_path):
        model = build_model(Facts.from_payload(valuation_payload()), "1.0")
        for fld in list(model.series):
            model.series[fld] = {k: v for k, v in model.series[fld].items()
                                 if not k.startswith("FY")}
        ws = self._export(tmp_path, model)
        assert "no annual filing was extracted" in self._text(ws)


class TestOwnBandOmitsMeaninglessPrices:
    """A negative implied price is a loss-making denominator, not a valuation."""

    def _export(self, tmp_path, own_band):
        model = build_model(Facts.from_payload(valuation_payload()), "1.0")
        valuation = value_company(model, Forecast.from_payload(FORECAST),
                                  Assumptions.from_payload(ASSUMPTIONS))
        payload = valuation.to_dict()
        payload.setdefault("comps", {})["own_band"] = own_band
        out = export_model_xlsx(model, payload, tmp_path / "model.xlsx")
        return openpyxl.load_workbook(out)["Company Comparison"]

    def test_negative_implied_price_is_dropped_and_explained(self, tmp_path):
        ws = self._export(tmp_path, {
            "ev_ebitda": {"band": [10.0, 16.0], "implied_price": [16.83, 26.49]},
            "p_e": {"band": [20.0, 30.0], "implied_price": [-0.60, -1.31]},
        })
        text = " ".join(str(c.value) for c in _cells(ws))
        assert "16.83" in text
        assert "-0.6" not in text and "-1.31" not in text
        assert "p_e omitted" in text

    def test_band_columns_are_labelled(self, tmp_path):
        ws = self._export(tmp_path, {
            "ev_ebitda": {"band": [10.0, 16.0], "implied_price": [16.83, 26.49]},
        })
        header = [c.value for row in ws.iter_rows() for c in row
                  if c.value in ("Low", "High")]
        assert header == ["Low", "High"]


class TestBridgeInputsAreNotMandateAssumptions:
    """Net debt and share count come off the model, at the latest period.

    They used to sit under "Assumptions (mandate)" with no period, so a reader
    comparing the DCF's net debt against the Statements Model's FY column saw
    two different numbers and no way to tell which was wrong.
    """

    def _dcf(self, tmp_path, **overrides):
        model = build_model(Facts.from_payload(valuation_payload()), "1.0")
        valuation = value_company(model, Forecast.from_payload(FORECAST),
                                  Assumptions.from_payload(ASSUMPTIONS))
        payload = valuation.to_dict()
        payload["dcf"].update(overrides)
        out = export_model_xlsx(model, payload, tmp_path / "model.xlsx")
        return openpyxl.load_workbook(out)["DCF"]

    def _text(self, ws):
        return " ".join(str(c.value) for c in _cells(ws))

    def test_net_debt_and_shares_carry_their_period(self, tmp_path):
        ws = self._dcf(tmp_path, net_debt_period="2026-Q3", shares_period="2026-Q3")
        text = self._text(ws)
        assert "Net debt (2026-Q3)" in text
        assert "Diluted shares, mm (2026-Q3)" in text
        assert "Bridge inputs (latest balance sheet)" in text

    def test_the_period_note_explains_the_disagreement_with_the_statements_tab(self, tmp_path):
        ws = self._dcf(tmp_path, net_debt_period="2026-Q3")
        assert "Statements Model reports it per fiscal year" in self._text(ws)

    def test_no_period_means_no_invented_label(self, tmp_path):
        ws = self._dcf(tmp_path, net_debt_period=None, shares_period=None)
        text = self._text(ws)
        assert "Net debt (" not in text
        assert "Statements Model reports it per fiscal year" not in text
