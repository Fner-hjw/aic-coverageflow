"""Tests for valuation_engine@2.0.

Fixture hand math (fiscal-year fallback base, FY2025 only, tax 15%):

  revenue 52,000  EBIT 2,080 (margin 0.04)  D&A 1,200 (0.0230769 of revenue)
  OCF 4,160  capex 1,560 (0.03 of revenue)
  blended growth 0.09 (segment weights 24k/12k/6k/6k/6k/4k, growth as FORECAST)
  ufcf_t = revenue_t * (0.04 * (1 - 0.15) + 1200/52000 - 0.03)
  integer exponents 1..5 (no period-end dates in the fixture)
  EBITDA_t = revenue_t * (0.04 + 1200/52000); exit multiple 10x; WACC 10%; g 2%
"""

import pytest

from coverageflow.model_engine import Facts, build_model
from coverageflow.valuation_engine import (
    Assumptions,
    BoundsViolation,
    Forecast,
    MissingFactError,
    value_company,
)

from coverageflow.gates import gate_terminal_reconciliation
from coverageflow.model_engine import period_ends_from_manifest, slugify

from . import facts_helpers as fh
from .test_model_engine import mini_facts_payload


# --------------------------------------------------------------- fixture

FY2025 = {
    "income_statement": {
        "revenue": 52000.0, "cost_of_revenue": 33800.0, "gross_profit": 18200.0,
        "operating_income": 2080.0, "depreciation_and_amortization": 1200.0,
        "net_income": 1040.0, "diluted_eps": 0.20, "diluted_shares_outstanding": 5200.0,
    },
    "cash_flow": {"operating_cash_flow": 4160.0, "capital_expenditure": 1560.0},
    "balance_sheet": {"total_assets": 140000.0, "total_stockholders_equity": 64000.0},
    # canonical segment facts; revenue weights sum 58,000, residual -6,000
    "segments": {
        "ccg": (24000.0, 1500.0), "dcai": (12_000.0, 600.0), "nex": (6000.0, 150.0),
        "foundry": (6000.0, -200.0), "altera": (6000.0, 150.0), "mobileye": (4000.0, 100.0),
        "all_other": (-6000.0, -220.0),
    },
}


def valuation_payload() -> dict:
    payload = mini_facts_payload()
    facts = payload["facts"]
    for group, table in (("income_statement", FY2025["income_statement"]),
                        ("cash_flow", FY2025["cash_flow"]),
                        ("balance_sheet", FY2025.get("balance_sheet", {}))):
        for fld, v in table.items():
            facts.append(fh.fact(f"{group}.{fld}", "FY2025", v))
    for seg, (rev, oi) in FY2025["segments"].items():
        facts.append(fh.fact(
            "segments.all_other_revenue" if seg == "all_other" else f"segments.{seg}.revenue",
            "FY2025", rev))
        facts.append(fh.fact(
            "segments.all_other_operating_income" if seg == "all_other"
            else f"segments.{seg}.operating_income",
            "FY2025", oi))
    return payload


FORECAST = {
    "role": "model", "horizon_years": 5, "terminal_basis": "mandate",
    "segments": [
        {"segment": "ccg", "growth": [0.08] * 5},
        {"segment": "dcai", "growth": [0.16] * 5},
        {"segment": "nex", "growth": [0.05] * 5},
        {"segment": "foundry", "growth": [0.05] * 5},
        {"segment": "altera", "growth": [0.05] * 5},
        {"segment": "mobileye", "growth": [0.12] * 5},
    ],
    "margin_glide": {"gross": [0.36, 0.37, 0.38, 0.39, 0.40]},
}

ASSUMPTIONS = {
    "as_of": "2026-10-30",
    "wacc": 0.10, "terminal_growth": 0.02, "exit_multiple_ebitda": 10.0,
    "discount_convention": "year_end", "tax_rate": 0.15,
    "forecast_bounds": {
        "segments": {"CCG": [-0.10, 0.12], "DCAI": [-0.05, 0.25], "NEX": [-0.05, 0.20],
                     "FOUNDRY": [-0.05, 0.30], "ALTERA": [-0.10, 0.15], "MOBILEYE": [-0.05, 0.20]},
        "margin_floor_gross": 0.30, "margin_ceiling_gross": 0.55,
    },
    "peer_block": {
        "as_of": "2026-10-30",
        "peers": [{"ticker": "PEER1", "ev_ebitda": 12.0, "p_e_ttm": 20.0, "p_s": 3.0,
                   "rev_growth_yoy": 0.12, "notional_note": "dated peer block"}],
    },
    "own_band": {"ev_ebitda": [8.0, 12.0], "p_e": [15.0, 25.0], "p_s": [2.0, 4.0]},
    "market": {"price": 5.00, "as_of": "2026-10-30"},
    "dcf_comps_tolerance": 0.25,
    "sensitivity": {"wacc_points": [0.08, 0.09, 0.10, 0.11, 0.12],
                    "terminal_growth_points": [0.01, 0.02, 0.03]},
}


@pytest.fixture
def valuation():
    model = build_model(Facts.from_payload(valuation_payload()), "1.0")
    return value_company(model, Forecast.from_payload(FORECAST),
                         Assumptions.from_payload(ASSUMPTIONS))



UFCF_PCT = 0.04 * 0.85 + 1200 / 52000 - 0.03
EBITDA_PCT = 0.04 + 1200 / 52000


@pytest.fixture
def valuation():
    model = build_model(Facts.from_payload(valuation_payload()), "1.0")
    return value_company(model, Forecast.from_payload(FORECAST),
                         Assumptions.from_payload(ASSUMPTIONS))


def _model(series: dict, slugs=(), period_ends=None) -> dict:
    return {"series": series, "segment_slugs": list(slugs),
            "period_ends": period_ends or {}, "candidate_id": "cand-x"}


# Intel-like synthetic series: FY2025, 9M FY2026 and 9M FY2025 columns
def _ltm_series(prior_missing=False) -> dict:
    def tri(fy, cur, prior):
        d = {"FY2025": fy, "2026-Q3-YTD": cur}
        if not prior_missing:
            d["2025-Q3-YTD"] = prior
        return d
    return {
        "income_statement.revenue": tri(52853.0, 46218.0, 39179.0),
        "income_statement.gross_profit": tri(20000.0, 18000.0, 15000.0),
        "income_statement.operating_income": tri(-1000.0, 2500.0, 1000.0),
        "cash_flow.depreciation_amortization": tri(10000.0, 8000.0, 7000.0),
        "cash_flow.operating_cash_flow": tri(9000.0, 13000.0, 8000.0),
        "cash_flow.capital_expenditure": tri(14000.0, 10000.0, 12000.0),
        "income_statement.net_income": tri(-500.0, 900.0, 300.0),
        "balance_sheet.short_term_debt": {"2026-Q3": 3000.0},
        "balance_sheet.long_term_debt": {"2026-Q3": 47000.0},
        "balance_sheet.cash_and_equivalents": {"2026-Q3": 20000.0},
        "balance_sheet.short_term_investments": {"2026-Q3": 30000.0},
        "income_statement.diluted_shares_outstanding": {"2026-Q3": 4000.0},
    }


TOTAL_FORECAST = {"horizon_years": 5, "segments": [{"segment": "total", "growth": [0.05] * 5}],
                  "margin_glide": {"gross": [0.40] * 5}}
ENDS = {"FY2025": "2025-12-27", "2026-Q3": "2026-09-26", "2026-Q3-YTD": "2026-09-26",
        "2025-Q3-YTD": "2025-09-27"}


def _value(model, forecast=TOTAL_FORECAST, **over):
    return value_company(model, forecast, {**ASSUMPTIONS, **over}).to_dict()


# --------------------------------------------------------------- DCF math

def test_blended_growth_is_hand_math(valuation):
    assert valuation.to_dict()["dcf"]["blended_growth"] == pytest.approx([0.09] * 5)
    blend = valuation.to_dict()["dcf"]["blend"]
    assert blend["method"] == "fiscal_year_segments"
    assert blend["weights"]["ccg"] == pytest.approx(24000 / 58000)


def test_base_is_the_fiscal_year_fallback_when_no_ytd_pair_exists(valuation):
    base = valuation.to_dict()["dcf"]["base"]
    assert base["method"] == "fiscal_year_fallback"
    assert base["window"] == {"fy": "FY2025", "ytd_current": None, "ytd_prior": None}
    assert "2026-Q3" in base["reason"]
    assert base["revenue"] == 52000.0 and base["ebit"] == 2080.0
    assert base["da"] == 1200.0 and base["ebitda"] == 3280.0
    assert base["capex"] == 1560.0 and base["fcf"] == 2600.0
    assert "annualisation" not in base


def test_dcf_projection_and_pv_hand_math(valuation):
    dcf = valuation.to_dict()["dcf"]
    rev = 52000.0
    pv = 0.0
    for t, y in enumerate(dcf["years"], start=1):
        rev *= 1.09
        assert y["revenue"] == pytest.approx(rev)
        assert y["ebit"] == pytest.approx(rev * 0.04)
        assert y["nopat"] == pytest.approx(rev * 0.04 * 0.85)
        assert y["da"] == pytest.approx(rev * 1200 / 52000)
        assert y["capex"] == pytest.approx(rev * 0.03)
        assert y["nwc_change"] == 0.0
        assert y["ufcf"] == pytest.approx(rev * UFCF_PCT)
        assert y["ebitda"] == pytest.approx(rev * EBITDA_PCT)
        assert y["discount_factor"] == pytest.approx(1.1 ** -t)
        pv += rev * UFCF_PCT * 1.1 ** -t
    assert dcf["pv_sum"] == pytest.approx(pv)
    assert dcf["discounting"]["discount_timing"] == "integer_fallback"
    assert dcf["years"][0]["revenue"] == pytest.approx(56680.0)


def test_gross_margin_glide_no_longer_drives_cash_flow():
    model = build_model(Facts.from_payload(valuation_payload()), "1.0")
    hi = {**FORECAST, "margin_glide": {"gross": [0.50] * 5}}
    a = value_company(model, FORECAST, ASSUMPTIONS).to_dict()["dcf"]
    b = value_company(model, hi, ASSUMPTIONS).to_dict()["dcf"]
    assert [y["ufcf"] for y in a["years"]] == pytest.approx([y["ufcf"] for y in b["years"]])
    assert b["years"][0]["gross_margin"] == 0.50


def test_tax_rate_is_applied_to_ebit():
    model = build_model(Facts.from_payload(valuation_payload()), "1.0")
    lo = value_company(model, FORECAST, {**ASSUMPTIONS, "tax_rate": 0.0}).to_dict()["dcf"]
    hi = value_company(model, FORECAST, {**ASSUMPTIONS, "tax_rate": 0.30}).to_dict()["dcf"]
    for a, b in zip(lo["years"], hi["years"]):
        assert a["ufcf"] - b["ufcf"] == pytest.approx(a["ebit"] * 0.30)
        assert b["nopat"] == pytest.approx(b["ebit"] * 0.70)
    assert hi["cash_flow_build"]["tax_rate"] == 0.30


def test_missing_tax_rate_raises():
    bad = {k: v for k, v in ASSUMPTIONS.items() if k != "tax_rate"}
    with pytest.raises(MissingFactError, match="tax_rate"):
        Assumptions.from_payload(bad)


def test_nwc_charge_on_incremental_revenue():
    model = build_model(Facts.from_payload(valuation_payload()), "1.0")
    dcf = value_company(model, FORECAST,
                        {**ASSUMPTIONS, "nwc_pct_incremental": 0.10}).to_dict()["dcf"]
    prev = 52000.0
    for y in dcf["years"]:
        assert y["nwc_change"] == pytest.approx(0.10 * (y["revenue"] - prev))
        assert y["ufcf"] == pytest.approx(y["nopat"] + y["da"] - y["capex"] - y["nwc_change"])
        prev = y["revenue"]
    assert dcf["cash_flow_build"]["nwc_pct_incremental"] == 0.10


def test_proposal_margin_and_capex_paths_replace_the_flat_ltm_values():
    model = build_model(Facts.from_payload(valuation_payload()), "1.0")
    f = {**FORECAST, "operating_margin_path": [0.05, 0.06, 0.07, 0.08, 0.09],
         "capex_pct_revenue_path": [0.04, 0.04, 0.035, 0.03, 0.03]}
    dcf = value_company(model, f, ASSUMPTIONS).to_dict()["dcf"]
    assert [y["ebit_margin"] for y in dcf["years"]] == [0.05, 0.06, 0.07, 0.08, 0.09]
    y1 = dcf["years"][0]
    assert y1["capex"] == pytest.approx(y1["revenue"] * 0.04)
    assert dcf["cash_flow_build"]["ebit_margin_source"] == "proposal.operating_margin_path"
    assert dcf["cash_flow_build"]["proposal_paths"] == {
        "operating_margin_path": "unbounded", "capex_pct_revenue_path": "unbounded"}


def test_proposal_paths_are_checked_against_declared_mandate_bounds():
    model = build_model(Facts.from_payload(valuation_payload()), "1.0")
    bounds = {**ASSUMPTIONS["forecast_bounds"], "operating_margin_floor": 0.0,
              "operating_margin_ceiling": 0.10, "capex_pct_bounds": [0.01, 0.06]}
    a = {**ASSUMPTIONS, "forecast_bounds": bounds}
    ok = {**FORECAST, "operating_margin_path": [0.05] * 5, "capex_pct_revenue_path": [0.03] * 5}
    dcf = value_company(model, ok, a).to_dict()["dcf"]
    assert dcf["cash_flow_build"]["proposal_paths"]["operating_margin_path"] == "bounded"
    with pytest.raises(BoundsViolation, match="operating_margin_path"):
        value_company(model, {**ok, "operating_margin_path": [0.20] * 5}, a)
    with pytest.raises(BoundsViolation, match="capex_pct_revenue_path"):
        value_company(model, {**ok, "capex_pct_revenue_path": [0.09] * 5}, a)


def test_ebitda_and_exit_multiple_terminal_hand_math(valuation):
    t = valuation.to_dict()["dcf"]["terminal"]
    rev5 = 52000 * 1.09 ** 5
    assert t["exit_multiple"]["ebitda_final"] == pytest.approx(rev5 * EBITDA_PCT)
    assert t["exit_multiple"]["terminal_value"] == pytest.approx(rev5 * EBITDA_PCT * 10)
    assert t["exit_multiple"]["pv"] == pytest.approx(rev5 * EBITDA_PCT * 10 / 1.1 ** 5)
    assert t["gordon"]["terminal_value"] == pytest.approx(rev5 * UFCF_PCT * 1.02 / 0.08)


# --------------------------------------------------- terminal reconciliation

def test_terminal_reconciliation_and_implied_figures(valuation):
    t = valuation.to_dict()["dcf"]["terminal"]
    ebitda5 = 52000 * 1.09 ** 5 * EBITDA_PCT
    ufcf5 = 52000 * 1.09 ** 5 * UFCF_PCT
    tv_x, tv_g = ebitda5 * 10, ufcf5 * 1.02 / 0.08
    assert t["implied_exit_multiple_from_gordon"] == pytest.approx(tv_g / ebitda5)
    ig = t["implied_growth_from_exit"]
    assert ig == pytest.approx((tv_x * 0.10 - ufcf5) / (tv_x + ufcf5))
    # the implied growth reproduces the exit value under Gordon
    assert ufcf5 * (1 + ig) / (0.10 - ig) == pytest.approx(tv_x)
    rec = t["reconciliation"]
    assert rec["ratio"] == pytest.approx(tv_x / tv_g)
    assert rec["tolerance"] == 0.35
    assert rec["within"] is False   # exit value is ~2.7x the Gordon value here


def test_reconciliation_within_with_matching_terminals_and_custom_tolerance():
    model = build_model(Facts.from_payload(valuation_payload()), "1.0")
    # pick the exit multiple that equals the Gordon value
    ebitda5 = 52000 * 1.09 ** 5 * EBITDA_PCT
    ufcf5 = 52000 * 1.09 ** 5 * UFCF_PCT
    mult = ufcf5 * 1.02 / 0.08 / ebitda5
    v = value_company(model, FORECAST, {**ASSUMPTIONS, "exit_multiple_ebitda": mult * 1.1,
                                        "terminal_tolerance": 0.15}).to_dict()
    rec = v["dcf"]["terminal"]["reconciliation"]
    assert rec["within"] is True and rec["tolerance"] == 0.15
    assert rec["ratio"] == pytest.approx(1.1)


def test_gate_terminal_reconciliation_pass_and_fail():
    model = build_model(Facts.from_payload(valuation_payload()), "1.0")
    ebitda5 = 52000 * 1.09 ** 5 * EBITDA_PCT
    ufcf5 = 52000 * 1.09 ** 5 * UFCF_PCT
    mult = ufcf5 * 1.02 / 0.08 / ebitda5
    good = value_company(model, FORECAST,
                         {**ASSUMPTIONS, "exit_multiple_ebitda": mult}).to_dict()
    ok, findings = gate_terminal_reconciliation(good, {"assumptions": ASSUMPTIONS})
    assert ok and findings == []
    bad = value_company(model, FORECAST, ASSUMPTIONS).to_dict()
    ok, findings = gate_terminal_reconciliation(bad, {"assumptions": ASSUMPTIONS})
    assert not ok and len(findings) == 1
    msg = findings[0]["summary"]
    t = bad["dcf"]["terminal"]
    assert f"{t['exit_multiple']['terminal_value']:,.0f}" in msg
    assert f"{t['gordon']['terminal_value']:,.0f}" in msg
    assert f"{t['implied_exit_multiple_from_gordon']:.1f}x" in msg
    assert f"{t['implied_growth_from_exit']:.2%}" in msg
    assert findings[0]["gate"] == "terminal_reconciliation"


def test_gate_flags_an_artifact_without_the_block():
    ok, findings = gate_terminal_reconciliation({"dcf": {}}, {})
    assert not ok and findings


# ------------------------------------------------------------ EV -> equity

def test_ev_to_per_share_hand_math(valuation):
    dcf = valuation.to_dict()["dcf"]
    b = dcf["bridge"]
    assert b["total_debt"] == 52000.0 and b["cash_total"] == 25000.0
    assert b["nci"] == 0.0 and b["nci_source"] == "absent"
    assert b["period"] == "2026-Q3"
    assert dcf["diluted_shares"] == 4375.0
    for k in ("exit_multiple", "gordon"):
        assert dcf["equity_value"][k] == pytest.approx(dcf["ev"][k] - 52000 + 25000)
        assert dcf["per_diluted_share"][k] == pytest.approx(dcf["equity_value"][k] / 4375)


def test_nci_is_subtracted_in_the_bridge():
    payload = valuation_payload()
    payload["facts"].append(fh.fact("balance_sheet.noncontrolling_interests", "2026-Q3", 1500.0))
    model = build_model(Facts.from_payload(payload), "1.0")
    dcf = value_company(model, FORECAST, ASSUMPTIONS).to_dict()["dcf"]
    assert dcf["bridge"]["nci"] == 1500.0
    assert dcf["bridge"]["nci_source"] == "balance_sheet"
    assert dcf["equity_value"]["gordon"] == pytest.approx(
        dcf["ev"]["gordon"] - 52000 + 25000 - 1500)


# ------------------------------------------------------- target and rating

def _tp(price, **over):
    model = build_model(Facts.from_payload(valuation_payload()), "1.0")
    a = {**ASSUMPTIONS, "market": {"price": price}, **over}
    return value_company(model, FORECAST, a).to_dict()["dcf"]


def test_target_is_gordon_with_a_flag_when_terminals_disagree():
    d = _tp(5.0)
    assert d["target_price"] == d["per_diluted_share"]["gordon"]
    assert d["target_flag"] == "terminal_values_outside_tolerance"
    assert d["upside"] == pytest.approx(d["target_price"] / 5.0 - 1)


def test_target_is_the_mean_when_terminals_reconcile():
    model = build_model(Facts.from_payload(valuation_payload()), "1.0")
    ebitda5 = 52000 * 1.09 ** 5 * EBITDA_PCT
    ufcf5 = 52000 * 1.09 ** 5 * UFCF_PCT
    mult = ufcf5 * 1.02 / 0.08 / ebitda5
    d = value_company(model, FORECAST, {**ASSUMPTIONS, "exit_multiple_ebitda": mult * 1.1}
                      ).to_dict()["dcf"]
    ps = d["per_diluted_share"]
    assert d["target_price"] == pytest.approx((ps["exit_multiple"] + ps["gordon"]) / 2)
    assert d["target_flag"] is None


def test_rating_bands_default_and_custom():
    d = _tp(5.0)
    tp = d["target_price"]
    assert _tp(tp / 1.20)["rating"] == "Buy"      # upside +20%
    assert _tp(tp / 1.05)["rating"] == "Hold"     # +5%
    assert _tp(tp / 0.80)["rating"] == "Sell"     # -20%
    assert _tp(tp / 1.05, rating_bands={"buy": 0.03, "sell": -0.10})["rating"] == "Buy"
    assert _tp(tp / 0.95, rating_bands={"buy": 0.15, "sell": -0.02})["rating"] == "Sell"


def test_no_price_gives_null_upside_and_rating():
    model = build_model(Facts.from_payload(valuation_payload()), "1.0")
    a = {k: v for k, v in ASSUMPTIONS.items() if k != "market"}
    d = value_company(model, FORECAST, a).to_dict()["dcf"]
    assert d["upside"] is None and d["rating"] is None
    assert d["target_price"] is not None


# ------------------------------------------------------------ LTM base

def test_ltm_base_from_a_10q_trigger():
    d = _value(_model(_ltm_series(), period_ends=ENDS))["dcf"]
    b = d["base"]
    assert b["method"] == "ltm" and b["reason"] is None
    assert b["window"] == {"fy": "FY2025", "ytd_current": "2026-Q3-YTD",
                           "ytd_prior": "2025-Q3-YTD"}
    assert b["revenue"] == 52853 + 46218 - 39179 == 59892
    assert b["gross_profit"] == 20000 + 18000 - 15000
    assert b["ebit"] == -1000 + 2500 - 1000
    assert b["da"] == 10000 + 8000 - 7000
    assert b["ebitda"] == b["ebit"] + b["da"]
    assert b["ocf"] == 9000 + 13000 - 8000 and b["capex"] == 14000 + 10000 - 12000
    assert b["fcf"] == b["ocf"] - b["capex"]
    assert b["net_income"] == -500 + 900 - 300
    assert b["ebit_margin"] == pytest.approx(b["ebit"] / 59892)
    # year 1 starts from LTM revenue, above the nine months already reported
    assert d["years"][0]["revenue"] == pytest.approx(59892 * 1.05)


def test_ltm_base_from_a_10k_trigger_is_the_fiscal_year():
    series = _ltm_series()
    for fld in series:
        series[fld] = {k: v for k, v in series[fld].items() if "YTD" not in k}
    b = _value(_model(series, period_ends=ENDS))["dcf"]["base"]
    assert b["method"] == "fiscal_year" and b["reason"] is None
    assert b["window"]["fy"] == "FY2025" and b["window"]["ytd_current"] is None
    assert b["revenue"] == 52853.0 and b["ebit"] == -1000.0


def test_new_fiscal_year_beats_an_older_ytd():
    series = _ltm_series()
    series["income_statement.revenue"]["FY2026"] = 61000.0
    for fld in ("income_statement.operating_income", "cash_flow.depreciation_amortization",
                "cash_flow.operating_cash_flow", "cash_flow.capital_expenditure"):
        series[fld]["FY2026"] = 1.0
    b = _value(_model(series, period_ends={**ENDS, "FY2026": "2026-12-26"}))["dcf"]["base"]
    assert b["method"] == "fiscal_year" and b["window"]["fy"] == "FY2026"
    assert b["revenue"] == 61000.0


def test_missing_prior_ytd_falls_back_to_the_fy_and_says_so():
    b = _value(_model(_ltm_series(prior_missing=True), period_ends=ENDS))["dcf"]["base"]
    assert b["method"] == "fiscal_year_fallback"
    assert "2025-Q3" in b["reason"] and "missing" in b["reason"]
    # never mixed: every flow is the FY, not a blend of FY and YTD
    assert b["revenue"] == 52853.0 and b["ebit"] == -1000.0
    assert b["da"] == 10000.0 and b["capex"] == 14000.0 and b["ocf"] == 9000.0


def test_a_negative_fcf_year_is_not_swapped_for_another_period():
    series = _ltm_series(prior_missing=True)   # FY fcf = 9000 - 14000 < 0
    b = _value(_model(series, period_ends=ENDS))["dcf"]["base"]
    assert b["fcf"] == -5000.0 and b["method"] == "fiscal_year_fallback"


def test_missing_core_flow_raises():
    series = _ltm_series()
    del series["cash_flow.capital_expenditure"]
    with pytest.raises(MissingFactError, match="capital expenditure"):
        _value(_model(series, period_ends=ENDS))


# ------------------------------------------------------------ discounting

def test_exponents_come_from_the_period_end_and_valuation_dates():
    ends = {**ENDS, "2026-Q3-YTD": "2026-09-27"}
    d = _value(_model(_ltm_series(), period_ends=ends))["dcf"]
    disc = d["discounting"]
    assert disc["discount_timing"] == "dated"
    assert disc["ltm_end"] == "2026-09-27" and disc["valuation_date"] == "2026-10-30"
    assert disc["cash_flow_dates"][0] == "2027-09-27"
    from datetime import date
    e1 = (date(2027, 9, 27) - date(2026, 10, 30)).days / 365.25
    assert e1 == pytest.approx(0.909, abs=1e-3)
    assert disc["exponents"][0] == pytest.approx(e1)
    assert disc["exponents"][1] == pytest.approx(
        (date(2028, 9, 27) - date(2026, 10, 30)).days / 365.25)
    assert d["years"][0]["discount_factor"] == pytest.approx(1.10 ** -e1, rel=0.01) or True
    assert d["years"][0]["discount_factor"] == pytest.approx(
        (1 + ASSUMPTIONS["wacc"]) ** -e1)


def test_mid_year_exponent_is_half_a_year_earlier_and_never_negative():
    ends = {**ENDS, "2026-Q3-YTD": "2026-09-27"}
    ye = _value(_model(_ltm_series(), period_ends=ends))["dcf"]["discounting"]["exponents"]
    mid = _value(_model(_ltm_series(), period_ends=ends),
                 discount_convention="mid_year")["dcf"]["discounting"]["exponents"]
    assert mid[1] == pytest.approx(ye[1] - 0.5)
    assert mid[0] == pytest.approx(ye[0] - 0.5)      # 0.409
    late = _value(_model(_ltm_series(), period_ends=ends),
                  discount_convention="mid_year", as_of="2027-09-01")["dcf"]["discounting"]
    assert min(late["exponents"]) >= 0.0 and late["exponents"][0] == 0.0


def test_integer_exponents_are_recorded_when_no_dates_exist():
    d = _value(_model(_ltm_series()))["dcf"]["discounting"]
    assert d["discount_timing"] == "integer_fallback"
    assert d["exponents"] == [1.0, 2.0, 3.0, 4.0, 5.0]


def test_period_ends_from_manifest():
    manifest = {"documents": [
        {"doc_id": "intc-10k-fy2025", "form": "10-K", "period": "2025-12-27"},
        {"doc_id": "intc-10q-2026q3", "form": "10-Q", "period": "2026-09-26"},
        {"doc_id": "intc-8k-2026-10-29", "form": "8-K", "period": "2026-10-29"}]}
    assert period_ends_from_manifest(manifest) == {
        "FY2025": "2025-12-27", "2026-Q3": "2026-09-26", "2026-Q3-YTD": "2026-09-26"}


# --------------------------------------------------------------- segments

def _seg_facts(ticker, names, weights, doc_form="10q"):
    """Segment revenue facts for FY2025 / 9M FY2026 / 9M FY2025, one issuer."""
    facts = []
    for n, (fy, cur, pri) in zip(names, weights):
        for per, v in (("FY2025", fy), ("2026-Q3-YTD", cur), ("2025-Q3-YTD", pri)):
            f = fh.fact(f"segments.{n}.revenue", per, v)
            f["citation"]["doc_id"] = f"{ticker}-{doc_form}-2026q3"
            facts.append(f)
    return facts


def test_amd_segments_blend_from_the_mapping_table():
    from coverageflow.model_engine import Facts, build_model as bm
    names = ["Data Center", "Client and Gaming", "Embedded"]
    facts = _seg_facts("amd", names, [(10000, 9000, 6000), (8000, 6000, 5000), (2000, 1500, 1500)])
    m = bm(Facts.from_payload({"facts": facts}), "1.0")
    assert m.segment_slugs == ["data_center", "client_and_gaming", "embedded"]
    assert m.mapping_file == "segment-mapping-amd-v1.json"
    assert "segments.data_center.revenue" in m.series
    # LTM segment revenue = FY + YTD - prior YTD
    ltm = {"data_center": 10000 + 9000 - 6000, "client_and_gaming": 8000 + 6000 - 5000,
           "embedded": 2000 + 1500 - 1500}
    series = {**_ltm_series(), **{k: v for k, v in m.series.items() if k.startswith("segments.")}}
    f = {"horizon_years": 5, "margin_glide": {"gross": [0.5] * 5}, "segments": [
        {"segment": "Data Center", "growth": [0.20] * 5},
        {"segment": "Client and Gaming", "growth": [0.05] * 5},
        {"segment": "Embedded", "growth": [0.00] * 5}]}
    d = value_company({"series": series, "segment_slugs": m.segment_slugs, "period_ends": ENDS},
                      f, ASSUMPTIONS).to_dict()["dcf"]
    tot = sum(ltm.values())
    assert d["blend"]["method"] == "ltm_segments"
    assert d["blend"]["weights"]["data_center"] == pytest.approx(ltm["data_center"] / tot)
    assert d["blended_growth"][0] == pytest.approx(
        (ltm["data_center"] * 0.20 + ltm["client_and_gaming"] * 0.05) / tot)


def test_intel_mapping_slugs_and_aliases_come_from_the_table():
    from coverageflow.model_engine import Facts, build_model as bm
    f = fh.fact("segments.CCPG.revenue", "FY2025", 100.0)
    f["citation"]["doc_id"] = "intc-10k-fy2025"
    m = bm(Facts.from_payload({"facts": [f]}), "1.0")
    assert m.segment_slugs == ["ccg", "dcai", "nex", "foundry", "altera", "mobileye"]
    assert m.series["segments.ccg.revenue"]["FY2025"] == 100.0
    assert m.to_dict()["segment_slugs"] == m.segment_slugs


def test_blend_uses_one_common_period_when_ltm_segments_are_incomplete():
    series = {**_ltm_series(),
              "segments.a.revenue": {"2025-Q3-YTD": 300.0, "FY2025": 500.0},
              "segments.b.revenue": {"2025-Q3-YTD": 100.0}}
    f = {"horizon_years": 5, "margin_glide": {"gross": [0.5] * 5}, "segments": [
        {"segment": "a", "growth": [0.10] * 5}, {"segment": "b", "growth": [0.00] * 5}]}
    d = value_company({"series": series, "segment_slugs": ["a", "b"], "period_ends": ENDS},
                      f, ASSUMPTIONS).to_dict()["dcf"]
    assert d["blend"]["method"] == "latest_common_period"
    assert d["blend"]["period"] == "2025-Q3-YTD"
    assert d["blended_growth"][0] == pytest.approx(0.75 * 0.10)


def test_a_segment_missing_from_the_proposal_raises():
    model = build_model(Facts.from_payload(valuation_payload()), "1.0")
    f = {**FORECAST, "segments": [s for s in FORECAST["segments"] if s["segment"] != "nex"]}
    with pytest.raises(BoundsViolation, match="proposal omits segment nex"):
        value_company(model, f, ASSUMPTIONS)


def test_no_hardcoded_company_segments_remain():
    from coverageflow import model_engine, valuation_engine
    assert not hasattr(model_engine, "SEGMENT_SLUGS")
    assert not hasattr(valuation_engine, "SEGMENT_SLUGS")
    assert slugify("Client and Gaming") == "client_and_gaming"



# --------------------------------------------------------- relative / misc

def test_relative_valuation_uses_the_ltm_base_and_bridge(valuation):
    c = valuation.to_dict()["comps"]["applied"]
    # EV/EBITDA 12x on EBITDA 3,280, net debt 27,000 over 4,375 shares
    assert c["ev_ebitda"]["implied_price"] == pytest.approx((12 * 3280 - 27000) / 4375)
    assert c["p_e"]["implied_price"] == pytest.approx(1040 / 4375 * 20)
    assert c["p_s"]["implied_price"] == pytest.approx(3 * 52000 / 4375)


def test_cross_check_delta_flagged(valuation):
    v = valuation.to_dict()
    dcf_up = v["dcf"]["upside"]
    assert v["dcf"]["dcf_upside"] == dcf_up
    assert v["cross_check_delta"] == pytest.approx(dcf_up - v["comps"]["comps_upside"])
    assert v["within_tolerance"] is (abs(v["cross_check_delta"]) <= 0.25)


def test_cross_check_within_tolerance():
    model = build_model(Facts.from_payload(valuation_payload()), "1.0")
    wide = {**ASSUMPTIONS, "dcf_comps_tolerance": 50.0}
    v = value_company(model, FORECAST, wide).to_dict()
    assert v["within_tolerance"] is True
    assert "agree" in v["cross_check_note"]


@pytest.mark.parametrize("overrides", [{"market": {}}, {"peer_block": {"peers": []}}])
def test_cross_check_unavailable_without_comps_or_market_price(overrides):
    model = build_model(Facts.from_payload(valuation_payload()), "1.0")
    v = value_company(model, FORECAST, {**ASSUMPTIONS, **overrides}).to_dict()
    assert v["cross_check_delta"] is None and v["within_tolerance"] is None
    assert "unavailable" in v["cross_check_note"]


def test_sensitivity_centre_cell_is_the_gordon_value(valuation):
    v = valuation.to_dict()
    grid = v["sensitivity"]["grid"]
    assert grid[2][1] == pytest.approx(v["dcf"]["per_diluted_share"]["gordon"])
    assert grid[0][2] > grid[4][2]          # lower WACC, higher value
    assert grid[2][2] > grid[2][0]          # higher growth, higher value


def test_sensitivity_uses_dated_exponents():
    d = _value(_model(_ltm_series(), period_ends=ENDS))
    centre = d["sensitivity"]["grid"][2][1]
    assert centre == pytest.approx(d["dcf"]["per_diluted_share"]["gordon"])


# --------------------------------------------------------- bounds/errors

def test_forecast_growth_out_of_bounds_raises():
    from coverageflow.model_engine import Facts
    model = build_model(Facts.from_payload(valuation_payload()), "1.0")
    bad = {**FORECAST, "segments": [
        {"segment": "dcai", "growth": [0.30] * 5}]}  # mandate cap 0.25
    with pytest.raises(BoundsViolation, match="growth"):
        value_company(model, Forecast.from_payload(bad), Assumptions.from_payload(ASSUMPTIONS))


def test_margin_glide_out_of_bounds_raises():
    from coverageflow.model_engine import Facts
    model = build_model(Facts.from_payload(valuation_payload()), "1.0")
    bad = {**FORECAST, "margin_glide": {"gross": [0.90] * 5}}  # ceiling 0.55
    with pytest.raises(BoundsViolation, match="margin"):
        value_company(model, Forecast.from_payload(bad), Assumptions.from_payload(ASSUMPTIONS))


def test_gordon_g_above_wacc_raises():
    from coverageflow.model_engine import Facts
    model = build_model(Facts.from_payload(valuation_payload()), "1.0")
    bad = {**ASSUMPTIONS, "terminal_growth": 0.11, "wacc": 0.10}
    with pytest.raises(BoundsViolation, match="terminal growth"):
        value_company(model, Forecast.from_payload(FORECAST), Assumptions.from_payload(bad))


def test_horizon_length_mismatch_raises():
    from coverageflow.model_engine import Facts
    model = build_model(Facts.from_payload(valuation_payload()), "1.0")
    bad = {**FORECAST, "margin_glide": {"gross": [0.36, 0.37]}}
    with pytest.raises(BoundsViolation, match="horizon"):
        value_company(model, Forecast.from_payload(bad), Assumptions.from_payload(ASSUMPTIONS))


def test_missing_diluted_shares_raises():
    from coverageflow.model_engine import Facts
    payload = valuation_payload()
    payload["facts"] = [f for f in payload["facts"]
                        if f["field"] != "income_statement.diluted_shares_outstanding"]
    model = build_model(Facts.from_payload(payload), "1.0")
    with pytest.raises(MissingFactError, match="diluted shares"):
        value_company(model, Forecast.from_payload(FORECAST),
                      Assumptions.from_payload(ASSUMPTIONS))


def test_missing_revenue_raises():
    from coverageflow.model_engine import Facts
    payload = valuation_payload()
    payload["facts"] = [f for f in payload["facts"]
                        if f["field"] != "income_statement.revenue"]
    model = build_model(Facts.from_payload(payload), "1.0")
    with pytest.raises(MissingFactError, match="revenue"):
        value_company(model, Forecast.from_payload(FORECAST),
                      Assumptions.from_payload(ASSUMPTIONS))


# --------------------------------------------------------- conventions

def test_mid_year_convention_discounts_later():
    from coverageflow.model_engine import Facts
    from coverageflow.valuation_engine import Assumptions, Forecast, value_company
    model = build_model(Facts.from_payload(valuation_payload()), "1.0")
    a = {**ASSUMPTIONS, "discount_convention": "mid_year"}
    mid = value_company(model, Forecast.from_payload(FORECAST), Assumptions.from_payload(a))
    year_end = value_company(model, Forecast.from_payload(FORECAST),
                             Assumptions.from_payload(ASSUMPTIONS))
    years = mid.to_dict()["dcf"]["years"]
    # exponents 0.5, 1.5, ... 4.5
    assert [y["discount_factor"] for y in years] == pytest.approx(
        [1 / 1.1 ** (i + 0.5) for i in range(5)])
    assert mid.to_dict()["dcf"]["per_diluted_share"]["exit_multiple"] > \
        year_end.to_dict()["dcf"]["per_diluted_share"]["exit_multiple"]


def test_accepts_raw_dicts_and_is_deterministic():
    from coverageflow.model_engine import Facts
    model = build_model(Facts.from_payload(valuation_payload()), "1.0")
    v1 = value_company(model, FORECAST, ASSUMPTIONS)  # dicts accepted directly
    v2 = value_company(model, FORECAST, ASSUMPTIONS)
    assert v1.to_dict() == v2.to_dict()
    assert v1.to_dict()["engine"] == "valuation_engine@2.0"
    assert v1.to_dict()["candidate_id"] == "cand-abc"


