"""TDD test suite for model_engine (written FIRST; engine implemented after).

All expectations are HAND-COMPUTED from the tests/fixtures/mini-10q.htm
numbers (the same filing sliced in test_slicer.py):

  2026-Q3: revenue 13,500  cost 8,325  gross 5,175  R&D 3,150  SG&A 1,200
           opex 4,350  op income 825  net income 700  diluted EPS 0.16
           diluted shares 4,375  OCF 3,200  capex 2,500  D&A 500
           cash 25,000  ST debt 2,000  LT debt 50,000
  2025-Q3: revenue 12,500  cost 8,125  gross 4,375  op income 175
           net income 150  EPS 0.03  shares 5,000  OCF 1,800  capex 1,700

Hand math (see each test):
  gross margin 26Q3 = 5,175/13,500 = 0.3833333...   (26Q2 rev 13,000 for qoq)
  operating margin =   825/13,500 = 0.0611111...
  EBITDA = 825 + 500 = 1,325  -> margin 1,325/13,500 = 0.0981481...
  net margin = 700/13,500 = 0.0518518...
  FCF = 3,200 - 2,500 = 700 (2026-Q3); 1,800 - 1,700 = 100 (2025-Q3)
  net debt = 2,000 + 50,000 - 25,000 = 27,000
  EPS linkage 700/4,375 = 0.16 (matches reported -> coherent)
  revenue YoY = 13,500/12,500 - 1 = 0.08
  revenue QoQ (vs 2026-Q2 13,000) = 13,500/13,000 - 1 = 0.0384615385
  net income YoY = 700/150 - 1 = 3.6666...
  CCG share of revenue = 6,000/13,500 = 0.4444444...
  CCG operating margin = 700/6,000 = 0.1166666...
  segment sum check: 6,000+4,500+2,000+1,000+800+500 = 14,800
                     + all other -1,300 = 13,500 == revenue (coherent)
"""

import pytest

from coverageflow.model_engine import (
    BoundsViolation,
    Facts,
    MissingFactError,
    build_model,
)

from . import facts_helpers as fh


# ----------------------------------------------------------------- fixtures

def mini_facts_payload() -> dict:
    """facts.json payload echoing the hand-written mini-10q.htm numbers."""
    periods = ["2026-Q3", "2025-Q3"]
    IS = {
        "revenue": (13500.0, 12500.0),
        "cost_of_revenue": (8325.0, 8125.0),
        "gross_profit": (5175.0, 4375.0),
        "research_and_development": (3150.0, 3050.0),
        "selling_general_and_administrative": (1200.0, 1150.0),
        "operating_expenses": (4350.0, 4200.0),
        "operating_income": (825.0, 175.0),
        "depreciation_and_amortization": (500.0, 450.0),
        "net_income": (700.0, 150.0),
        "diluted_eps": (0.16, 0.03),
        "diluted_shares_outstanding": (4375.0, 5000.0),
    }
    BS = {
        "cash_and_equivalents": (25000.0, 21000.0),
        "short_term_debt": (2000.0, 2000.0),
        "long_term_debt": (50000.0, 48000.0),
        "total_assets": (145000.0, 140000.0),
        "total_stockholders_equity": (66000.0, 64000.0),
    }
    CF = {
        "operating_cash_flow": (3200.0, 1800.0),
        "capital_expenditure": (2500.0, 1700.0),
    }
    SEGMENTS = {  # as-reported names, translated via segment-mapping-v1
        "Client Computing Group": ((6000.0, 700.0), (5800.0, 500.0)),
        "Data Center and AI": ((4500.0, 300.0), (4000.0, 150.0)),
        "Network and Edge": ((2000.0, 150.0), (1700.0, 100.0)),
        "Intel Foundry": ((1000.0, -650.0), (1000.0, -700.0)),
        "Altera": ((800.0, 250.0), (750.0, 200.0)),
        "Mobileye": ((500.0, 100.0), (450.0, 50.0)),
        "All Other": ((-1300.0, -25.0), (-1200.0, -25.0)),
    }
    facts, i = [], 0
    for group, table in (("income_statement", IS), ("balance_sheet", BS), ("cash_flow", CF)):
        for fld, values in table.items():
            for p, v in zip(periods, values):
                facts.append(fh.fact(f"{group}.{fld}", p, v, idx=i))
                i += 1
    for name, ((r26, o26), (r25, o25)) in SEGMENTS.items():
        facts.append(fh.fact(f"segments.{name}.revenue", "2026-Q3", r26, idx=i)); i += 1
        facts.append(fh.fact(f"segments.{name}.revenue", "2025-Q3", r25, idx=i)); i += 1
        facts.append(fh.fact(f"segments.{name}.operating_income", "2026-Q3", o26, idx=i)); i += 1
        facts.append(fh.fact(f"segments.{name}.operating_income", "2025-Q3", o25, idx=i)); i += 1
    facts.append(fh.fact("income_statement.revenue", "2026-Q2", 13000.0, idx=i))
    return {"role": "extractor", "request_id": "req-0001", "candidate_id": "cand-abc",
            "facts": facts, "missing": [], "conformance": "schema 1.0"}


@pytest.fixture
def payload():
    return mini_facts_payload()


@pytest.fixture
def model(payload):
    return build_model(Facts.from_payload(payload), "1.0")


# ----------------------------------------------------------------- basics

def test_model_header_is_contract_conformant(model):
    d = model.to_dict()
    assert d["artifact"] == "model"
    assert d["engine"] == "model_engine@1.0"
    assert d["mapping_tables"] == "1.0"
    assert d["candidate_id"] == "cand-abc"
    assert d["input_facts_sha256"]
    assert "series" in d


def test_series_carries_raw_facts(model):
    d = model.to_dict()
    assert d["series"]["income_statement.revenue"]["2026-Q3"] == 13500.0
    assert d["series"]["income_statement.revenue"]["2025-Q3"] == 12500.0
    assert d["series"]["balance_sheet.total_assets"]["2026-Q3"] == 145000.0
    # older period does not inherit the newer value
    assert "2024-Q3" not in d["series"]["income_statement.revenue"]


def test_as_reported_segments_translated_to_canonical(model):
    d = model.to_dict()
    assert d["series"]["segments.ccg.revenue"]["2026-Q3"] == 6000.0
    assert d["series"]["segments.dcai.revenue"]["2026-Q3"] == 4500.0
    assert d["series"]["segments.nex.revenue"]["2026-Q3"] == 2000.0
    assert d["series"]["segments.foundry.revenue"]["2026-Q3"] == 1000.0
    assert d["series"]["segments.altera.revenue"]["2026-Q3"] == 800.0
    assert d["series"]["segments.mobileye.revenue"]["2026-Q3"] == 500.0
    # "All Other" is kept as the residual, not a canonical segment
    assert d["series"]["segments.all_other_revenue"]["2026-Q3"] == -1300.0
    assert d["series"]["segments.foundry.operating_income"]["2026-Q3"] == -650.0


# ----------------------------------------------------------- derived values

def test_derived_margins_hand_math(model):
    d = model.to_dict()
    s = d["series"]
    # 5,175/13,500 = 0.38333...
    assert s["derived.gross_margin"]["2026-Q3"] == pytest.approx(5175 / 13500)
    # 4,375/12,500 = 0.35
    assert s["derived.gross_margin"]["2025-Q3"] == pytest.approx(0.35)
    # 825/13,500 = 0.06111...
    assert s["derived.operating_margin"]["2026-Q3"] == pytest.approx(825 / 13500)
    # EBITDA = 825 + 500 = 1,325; margin 1,325/13,500 = 0.0981481...
    assert s["income_statement.ebitda"]["2026-Q3"] == pytest.approx(1325.0)
    assert s["derived.ebitda_margin"]["2026-Q3"] == pytest.approx(1325 / 13500)
    # 700/13,500 = 0.0518518...
    assert s["derived.net_margin"]["2026-Q3"] == pytest.approx(700 / 13500)


def test_derived_fcf_hand_math(model):
    # 3,200 - 2,500 = 700 ; 1,800 - 1,700 = 100
    d = model.to_dict()
    assert d["series"]["derived.fcf"]["2026-Q3"] == 700.0
    assert d["series"]["derived.fcf"]["2025-Q3"] == 100.0


def test_derived_net_debt_hand_math(model):
    # 2,000 + 50,000 - 25,000 = 27,000
    d = model.to_dict()
    assert d["series"]["derived.net_debt"]["2026-Q3"] == 27000.0


def test_growth_yoy_qoq_hand_math(model):
    d = model.to_dict()
    rev = d["series"]["income_statement.revenue"]
    # 13,500/12,500 - 1 = 0.08
    assert rev["yoy"]["2026-Q3"] == pytest.approx(0.08)
    assert rev["yoy_computed"] is True
    # 13,500/13,000 - 1 = 0.0384615385
    assert rev["qoq"]["2026-Q3"] == pytest.approx(13500 / 13000 - 1)
    # no 2024-Q3 base -> 2025-Q3 yoy absent, not zero
    assert "2025-Q3" not in rev["yoy"]
    # 700/150 - 1 = 3.6666...
    ni = d["series"]["income_statement.net_income"]
    assert ni["yoy"]["2026-Q3"] == pytest.approx(700 / 150 - 1)


def test_segment_derived_metrics_hand_math(model):
    d = model.to_dict()
    # CCG margin 700/6,000 = 0.1166666...; share 6,000/13,500 = 0.4444444...
    assert d["series"]["segments.ccg.operating_margin"]["2026-Q3"] == pytest.approx(700 / 6000)
    assert d["series"]["segments.ccg.revenue_share_of_total"]["2026-Q3"] == pytest.approx(6000 / 13500)
    # DCAI margin 300/4,500 = 0.0666666...
    assert d["series"]["segments.dcai.operating_margin"]["2026-Q3"] == pytest.approx(300 / 4500)


def test_fiscal_year_yoy():
    """FY periods compare to prior FY."""
    payload = mini_facts_payload()
    payload["facts"] = [
        fh.fact("income_statement.revenue", "FY2025", 54000.0, idx=0),
        fh.fact("income_statement.revenue", "FY2024", 54000.0 / 1.125, idx=1),
    ]
    model = build_model(Facts.from_payload(payload), "1.0")
    rev = model.to_dict()["series"]["income_statement.revenue"]
    assert rev["yoy"]["FY2025"] == pytest.approx(0.125)


# ------------------------------------------------------------ coherence

def test_eps_linkage_and_segment_coherence_pass(model):
    """Fixture is coherent: 700/4,375 = 0.16 (reported);
    segments 14,800 + all other -1,300 = 13,500 = revenue."""
    d = model.to_dict()
    assert d["checks"]["eps_linkage"]["2026-Q3"] == "ok"
    assert d["checks"]["segment_revenue_reconciliation"]["2026-Q3"] == "ok"
    assert d["checks"]["gross_profit_reconciliation"]["2026-Q3"] == "ok"


def test_eps_mismatch_raises_bounds_violation():
    payload = mini_facts_payload()
    for f in payload["facts"]:
        if f["field"] == "income_statement.diluted_eps" and f["period"] == "2026-Q3":
            f["value"] = 0.50  # 700/4,375 = 0.16, not 0.50
    with pytest.raises(BoundsViolation, match="EPS"):
        build_model(Facts.from_payload(payload), "1.0")


def test_segment_sum_mismatch_raises_bounds_violation():
    payload = mini_facts_payload()
    for f in payload["facts"]:
        if f["field"] == "segments.Client Computing Group.revenue" and f["period"] == "2026-Q3":
            f["value"] = 7000.0  # would make segments sum 15,800 - 1,300 = 14,500 != 13,500
    with pytest.raises(BoundsViolation, match="[Rr]econcil"):
        build_model(Facts.from_payload(payload), "1.0")


def test_negative_revenue_raises_bounds_violation():
    payload = mini_facts_payload()
    payload["facts"].append(fh.fact("income_statement.revenue", "2026-Q1", -100.0, idx=999))
    with pytest.raises(BoundsViolation, match="revenue"):
        build_model(Facts.from_payload(payload), "1.0")


def test_gross_profit_mismatch_raises_bounds_violation():
    payload = mini_facts_payload()
    for f in payload["facts"]:
        if f["field"] == "income_statement.gross_profit" and f["period"] == "2026-Q3":
            f["value"] = 6000.0  # 13,500 - 8,325 = 5,175, not 6,000
    with pytest.raises(BoundsViolation, match="[Gg]ross"):
        build_model(Facts.from_payload(payload), "1.0")


# ------------------------------------------------------------ typed errors

def test_missing_required_fact_raises():
    payload = mini_facts_payload()
    payload["facts"] = [f for f in payload["facts"]
                        if f["field"] != "cash_flow.operating_cash_flow"]
    with pytest.raises(MissingFactError, match="cash_flow.operating_cash_flow"):
        build_model(Facts.from_payload(payload), "1.0",
                    required_fields=["cash_flow.operating_cash_flow"])


def test_deterministic_hash_and_output(payload):
    m1 = build_model(Facts.from_payload(payload), "1.0")
    m2 = build_model(Facts.from_payload(payload), "1.0")
    assert m1.to_dict() == m2.to_dict()
    assert m1.facts_sha == m2.facts_sha


def test_accepts_plain_dict_payload(payload):
    m1 = build_model(Facts.from_payload(payload), "1.0")
    m2 = build_model(payload, "1.0")  # convenience: dict accepted directly
    assert m1.to_dict() == m2.to_dict()
