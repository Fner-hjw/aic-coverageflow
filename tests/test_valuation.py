"""TDD test suite for valuation_engine (written FIRST; engine implemented after).

Hand-computed valuation on top of the mini-10q fixture numbers plus a
hand-authored FY2025 annual block:

  FY2025: revenue 52,000  COGS 33,800  gross 18,200 (GROSS MARGIN 0.35)
          operating income 2,080  D&A 1,200  EBITDA 3,280 (= 2,080+1,200)
          EBITDA margin = 3,280/52,000 = 0.0630769...
          net income 1,040  diluted EPS 0.20  diluted shares 5,200
          OCF 4,160  capex 3,120  => FCF 1,040  FCF margin = 0.02 EXACTLY
  Latest balance data (2026-Q3 from mini fixture): net debt 27,000
  Latest diluted shares (2026-Q3): 4,375
  Forecast: segment revenue weights (FY2025) sum 58,000:
          CCG 24,000 DCAI 12,000 NEX 6,000 Foundry 6,000 Altera 6,000 Mobileye 4,000
          growth CCG .08 DCAI .16 NEX .05 Foundry .05 Altera .05 Mobileye .12
  Blended growth = (24000*.08 + 12000*.16 + 3*6000*.05 + 4000*.12)/58,000
                 = (1920+1920+900+480)/58,000 = 5,220/58,000 = 0.09 EXACTLY
  Margin glide: gross [0.36, 0.37, 0.38, 0.39, 0.40]
          => FCF margin = 0.02 + (glide - 0.35) = [0.03, 0.04, 0.05, 0.06, 0.07]

  Revenue path (x1.09 each year):
          R1 56,680.00   R2 61,781.20   R3 67,341.508   R4 73,402.24372   R5 80,008.4457
  FCF path: 1,700.40 / 2,471.248 / 3,367.0754 / 4,404.1346 / 5,600.5912
  WACC = 0.10, terminal growth g = 0.02, exit multiple = 10.0x EBITDA
  EBITDA margin glide = 0.0630769 + (glide - 0.35):
          year5 = 5,880/52,000 = 0.1130769...  =>  EBITDA_5 = 9,047.1081

  Year-end discounting at 10%: 1.1, 1.21, 1.331, 1.4641, 1.61051
  PV(FCF) = 1,545.8182 + 2,042.3537 + 2,529.7336 + 3,008.0832 + 3,477.5264
          = 12,603.515 (approx)
  Exit-multiple terminal: TV = 90,471.081  PV(TV) = 56,175.42
          EV_exit = 68,778.94  equity = 41,778.94  per share = 9.5495
  Gordon terminal: TV = 5,600.5912 * 1.02/0.08 = 5,600.5912 * 12.75 = 71,407.538
          PV(TV) = 44,338.47  EV_gordon = 56,941.99  equity = 29,941.99
          per share = 6.8439

  Comps: market price 5.00 (mandate), peers P/E 20x, EV/EBITDA 12x, P/S 3x
          EV/EBITDA: (12*3,280 - 27,000)/4,375 = 12,360/4,375 = 2.8251
          P/E:      (1,040/4,375) * 20 = 4.7543
          P/S:       3 * 52,000/4,375 = 35.6571
          comps price = median = 4.7543  upside = 4.7543/5 - 1 = -0.04914

  Own band: EV/EBITDA [8,12] -> [-0.1737, 2.8251]; P/E [15,25] -> [3.5657, 5.9429]
            P/S [2,4] -> [23.7714, 47.5429]

  Cross-check: dcf upside (exit primary) = 9.5495/5 - 1 = 0.90989
               delta = 0.90989 - (-0.04914) = 0.95903  > tolerance 0.25 -> flagged

  Sensitivity (Gordon basis), cell wacc 0.10 / g 0.03:
          TV = 5,600.5912 * 1.03/0.07 = 82,408.699
          per share = (12,603.5152 + 82,408.699/1.61051 - 27,000)/4,375 = 8.4053
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

from . import facts_helpers as fh
from .test_model_engine import mini_facts_payload


# --------------------------------------------------------------- fixture

FY2025 = {
    "income_statement": {
        "revenue": 52000.0, "cost_of_revenue": 33800.0, "gross_profit": 18200.0,
        "operating_income": 2080.0, "depreciation_and_amortization": 1200.0,
        "net_income": 1040.0, "diluted_eps": 0.20, "diluted_shares_outstanding": 5200.0,
    },
    "cash_flow": {"operating_cash_flow": 4160.0, "capital_expenditure": 3120.0},
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


# --------------------------------------------------------------- DCF math

def test_blended_growth_is_hand_math(valuation):
    dcf = valuation.to_dict()["dcf"]
    # (1920 + 1920 + 900 + 480) / 58,000 = 0.09 exactly
    assert dcf["blended_growth"] == pytest.approx([0.09] * 5)
    assert dcf["base"]["period"] == "FY2025"
    assert dcf["base"]["revenue"] == 52000.0
    assert dcf["base"]["fcf_margin"] == pytest.approx(0.02)


def test_dcf_projection_and_pv_hand_math(valuation):
    years = valuation.to_dict()["dcf"]["years"]
    assert [y["revenue"] for y in years] == pytest.approx(
        [56680.0, 61781.20, 67341.508, 73402.24372, 80008.4456548])
    assert [y["fcf_margin"] for y in years] == pytest.approx(
        [0.03, 0.04, 0.05, 0.06, 0.07])
    assert [y["fcf"] for y in years] == pytest.approx(
        [1700.40, 2471.248, 3367.0754, 4404.1346232, 5600.591195836])
    # year-end discounting: 1/1.1, 1/1.21, ... 1/1.61051
    assert [y["discount_factor"] for y in years] == pytest.approx(
        [1 / 1.1 ** (i + 1) for i in range(5)])
    assert [y["pv"] for y in years] == pytest.approx(
        [1700.40 / 1.1, 2471.248 / 1.21, 3367.0754 / 1.331,
         4404.1346232 / 1.4641, 5600.591195836 / 1.61051], rel=1e-9)
    assert valuation.to_dict()["dcf"]["pv_sum"] == pytest.approx(12603.515, rel=1e-4)


def test_ebitda_and_exit_multiple_terminal_hand_math(valuation):
    d = valuation.to_dict()
    term = d["dcf"]["terminal"]
    # EBITDA_5 = 52,000 x 1.09^5 x 5,880/52,000 = 9,047.1081
    assert term["exit_multiple"]["ebitda_final"] == pytest.approx(9047.1081, rel=1e-6)
    assert term["exit_multiple"]["terminal_value"] == pytest.approx(90471.081, rel=1e-6)
    # PV(TV) = 90,471.081 / 1.61051 = 56,175.42
    assert term["exit_multiple"]["pv"] == pytest.approx(56175.42, rel=1e-5)
    # Gordon: 5,600.5912 x 1.02 / 0.08 = 71,407.538 ; PV = 44,338.47
    assert term["gordon"]["terminal_value"] == pytest.approx(71407.538, rel=1e-6)
    assert term["gordon"]["pv"] == pytest.approx(44338.47, rel=1e-5)


def test_ev_to_per_share_hand_math(valuation):
    dcf = valuation.to_dict()["dcf"]
    # EV_exit = 12,603.515 + 56,175.42 = 68,778.94 ; equity = 41,778.94
    assert dcf["ev"]["exit_multiple"] == pytest.approx(68778.94, rel=1e-4)
    assert dcf["equity_value"]["exit_multiple"] == pytest.approx(41778.94, rel=1e-4)
    assert dcf["per_diluted_share"]["exit_multiple"] == pytest.approx(9.5495, rel=1e-4)
    # Gordon variant: EV 56,941.99 ; per share 6.8439
    assert dcf["ev"]["gordon"] == pytest.approx(56941.99, rel=1e-4)
    assert dcf["per_diluted_share"]["gordon"] == pytest.approx(6.8439, rel=1e-4)
    assert dcf["net_debt"] == 27000.0
    assert dcf["diluted_shares"] == 4375.0


# --------------------------------------------------------------- comps

def test_relative_valuation_hand_math(valuation):
    comps = valuation.to_dict()["comps"]
    applied = comps["applied"]
    # (12x3,280 - 27,000)/4,375 = 2.82514...
    assert applied["ev_ebitda"]["implied_price"] == pytest.approx(12360 / 4375)
    # (1,040/4,375) x 20 = 4.75429...
    assert applied["p_e"]["implied_price"] == pytest.approx(1040 / 4375 * 20)
    # 3 x 52,000/4,375 = 35.65714...
    assert applied["p_s"]["implied_price"] == pytest.approx(3 * 52000 / 4375)
    # median of the three methods
    assert comps["comps_price"] == pytest.approx(1040 / 4375 * 20)
    # upside vs 5.00 market price
    assert comps["comps_upside"] == pytest.approx(1040 / 4375 * 20 / 5 - 1)


def test_own_historical_band(valuation):
    band = valuation.to_dict()["comps"]["own_band"]
    # EV/EBITDA [8,12] -> [(26,240-27,000)/4,375, 12,360/4,375]
    assert band["ev_ebitda"]["implied_price"] == pytest.approx([-760 / 4375, 12360 / 4375])
    # P/E [15,25] -> [3.56571..., 5.94286...]
    assert band["p_e"]["implied_price"] == pytest.approx([1040 / 4375 * 15, 1040 / 4375 * 25])
    # P/S [2,4] -> [23.7714..., 47.5429...]
    assert band["p_s"]["implied_price"] == pytest.approx([104000 / 4375, 208000 / 4375])


# ----------------------------------------------------------- cross-check

def test_cross_check_delta_flagged(valuation):
    d = valuation.to_dict()
    # dcf upside 0.90989 (exit primary); comps upside -0.04914; delta 0.95903
    assert d["dcf"]["dcf_upside"] == pytest.approx(9.5495 / 5 - 1, rel=1e-4)
    assert d["cross_check_delta"] == pytest.approx(0.95903, rel=1e-3)
    assert d["within_tolerance"] is False
    assert "human" in d["cross_check_note"] or "C2" in d["cross_check_note"]


def test_cross_check_within_tolerance():
    from coverageflow.model_engine import Facts
    from coverageflow.valuation_engine import Assumptions, Forecast, value_company
    model = build_model(Facts.from_payload(valuation_payload()), "1.0")
    a = {**ASSUMPTIONS, "dcf_comps_tolerance": 2.0}
    v = value_company(model, Forecast.from_payload(FORECAST), Assumptions.from_payload(a))
    assert v.to_dict()["within_tolerance"] is True


# ----------------------------------------------------------- sensitivity

def test_sensitivity_grid_hand_math(valuation):
    sens = valuation.to_dict()["sensitivity"]
    assert sens["wacc_points"] == [0.08, 0.09, 0.10, 0.11, 0.12]
    assert sens["terminal_growth_points"] == [0.01, 0.02, 0.03]
    grid = sens["grid"]  # rows = wacc points, cols = growth points
    assert len(grid) == 5 and all(len(r) == 3 for r in grid)
    # center cell (wacc .10, g .02) equals the Gordon per-share DCF
    assert grid[2][1] == pytest.approx(valuation.to_dict()["dcf"]["per_diluted_share"]["gordon"])
    # hand-computed cell (wacc .10, g .03): 8.4053
    cell = (12603.515231 + 5600.591195836 * 1.03 / 0.07 / 1.61051
            - 27000) / 4375
    assert grid[2][2] == pytest.approx(cell, rel=1e-4)


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
    assert v1.to_dict()["engine"] == "valuation_engine@1.0"
    assert v1.to_dict()["candidate_id"] == "cand-abc"
