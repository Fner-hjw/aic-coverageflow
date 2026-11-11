"""Prefill proposal builder: recorded fixtures only, no network."""

from __future__ import annotations

import json
import statistics
from pathlib import Path

import pytest

from coverageflow import mandate_check, mandate_prefill as mp
from tests.market_fixtures import CUTOFF, Recorded, world


def _mult(price, scale, shares=2.0, debt=20.0, cash=30.0):
    cap = price * shares
    rev = scale * (100 + 70 - 40) + 5      # restated +5 flows through every tag
    oi = scale * (10 + 7 - 4) + 5
    da = scale * (5 + 3.5 - 2) + 5
    ni = scale * (8 + 5 - 3) + 5
    return {"ev_ebitda": (cap + debt - cash) / (oi + da), "p_e_ttm": price / (ni / shares),
            "p_s": cap / rev}


@pytest.fixture()
def proposal():
    return mp.propose_mandate("INTC", CUTOFF, fetch=world(), peers=None)


def test_shape_status_and_schema(proposal):
    assert proposal["status"] == "proposed"
    assert mandate_check.schema_errors(proposal) == []
    assert proposal["cik"] == "0000050863"
    assert set(proposal) >= {"provenance", "missing", "warnings", "proposed_at"}
    for path, e in proposal["provenance"].items():
        assert {"value", "source", "url", "as_of", "sha256", "basis"} <= set(e), path


def test_price_and_provenance(proposal):
    a = proposal["assumptions"]
    assert a["market"]["price"] == 23.0 and a["market"]["as_of"] == "2026-10-30"
    e = proposal["provenance"]["assumptions.market.price"]
    assert e["source"] == "stooq" and e["sha256"] and e["as_of"] <= CUTOFF


def test_peers_from_reference_set_with_multiple_math(proposal):
    # INTC in references/peer-sets.json lists AMD, NVDA, TXN, QCOM, MU; only AMD and
    # NVDA have recorded data, the rest are skipped with a warning
    rows = proposal["assumptions"]["peer_block"]["peers"]
    assert [r["ticker"] for r in rows] == ["AMD", "NVDA"]
    amd = _mult(40.0, 2.0)
    assert rows[0]["ev_ebitda"] == pytest.approx(amd["ev_ebitda"], abs=0.006)
    assert rows[0]["p_e_ttm"] == pytest.approx(amd["p_e_ttm"], abs=0.006)
    assert rows[0]["p_s"] == pytest.approx(amd["p_s"], abs=0.006)
    assert any("TXN skipped" in w for w in proposal["warnings"])
    inp = proposal["provenance"]["assumptions.peer_block.peers.0.ev_ebitda"]["inputs"]
    assert inp["price"]["value"] == 40.0 and inp["revenue"]["tag"] == "us-gaap:Revenues"
    assert proposal["provenance"]["assumptions.peer_block.peers"]["source"] == "references/peer-sets.json"


def test_explicit_peers_override_and_incomplete_peer_skipped():
    w = world(peers=("AMD",))
    m = mp.propose_mandate("INTC", CUTOFF, fetch=w, peers=["amd", "ZZZ"])
    assert [r["ticker"] for r in m["assumptions"]["peer_block"]["peers"]] == ["AMD"]
    assert any("ZZZ skipped" in x for x in m["warnings"])
    assert m["provenance"]["assumptions.peer_block.peers"]["source"] == "user"


def test_exit_multiple_and_own_band_from_peers(proposal):
    rows = proposal["assumptions"]["peer_block"]["peers"]
    evs = [r["ev_ebitda"] for r in rows]
    a = proposal["assumptions"]
    assert a["exit_multiple_ebitda"] == round(statistics.median(evs), 1)
    assert a["own_band"]["ev_ebitda"] == [min(evs), max(evs)]


def test_wacc_capm_and_every_input_recorded(proposal):
    rf, beta, erp, spread = 0.042, 1.7, 0.05, 0.015
    tax = proposal["assumptions"]["tax_rate"]
    assert tax == 0.2                      # effective rate 2 / 10 is sane
    e, d = 23.0 * 2.0, 20.0
    expect = (e * (rf + beta * erp) + d * (rf + spread) * (1 - tax)) / (e + d)
    a = proposal["assumptions"]
    assert a["wacc"] == pytest.approx(expect, abs=6e-5)
    inp = proposal["provenance"]["assumptions.wacc"]["inputs"]
    assert inp["risk_free"]["value"] == pytest.approx(rf) and inp["risk_free"]["sha256"]
    assert inp["beta"]["used"] == pytest.approx(1.7, abs=1e-3) and not inp["beta"]["clamped"]
    assert inp["beta"]["benchmark"] == "SPY" and inp["beta"]["n_weekly_returns"] == 104
    assert inp["equity_risk_premium"]["value"] == erp
    assert inp["weights"]["equity"] == pytest.approx(e / (e + d), abs=1e-4)
    assert a["sensitivity"]["wacc_points"][2] == a["wacc"]


def test_erp_and_spread_are_configurable():
    a = mp.propose_mandate("INTC", CUTOFF, fetch=world(), erp=0.06, debt_spread=0.02)
    b = mp.propose_mandate("INTC", CUTOFF, fetch=world())
    assert a["assumptions"]["wacc"] > b["assumptions"]["wacc"]
    assert a["provenance"]["assumptions.wacc"]["inputs"]["equity_risk_premium"]["value"] == 0.06


def test_beta_is_clamped_and_flagged():
    m = mp.propose_mandate("INTC", CUTOFF, fetch=world(beta=4.0))
    inp = m["provenance"]["assumptions.wacc"]["inputs"]["beta"]
    assert inp["clamped"] and inp["used"] == 2.5 and inp["raw"] == pytest.approx(4.0, abs=1e-3)
    assert any("clamped" in w for w in m["warnings"])


def test_defaults_kept_and_segments_fall_back_with_a_basis(proposal):
    a = proposal["assumptions"]
    assert a["terminal_growth"] == 0.025 and a["dcf_comps_tolerance"] == 0.25
    assert a["forecast_bounds"]["segments"]["CCG"] == [-0.10, 0.12]
    assert "not in SEC companyfacts" in proposal["provenance"][
        "assumptions.forecast_bounds.segments.CCG"]["basis"]
    # gross margin FY24 0.5, FY25 0.55 widened by 0.05
    assert a["forecast_bounds"]["margin_floor_gross"] == 0.45
    assert a["forecast_bounds"]["margin_ceiling_gross"] == 0.6


def test_nothing_is_dated_after_the_cutoff(proposal):
    for path, e in proposal["provenance"].items():
        if e.get("as_of") and path != "cik":
            assert e["as_of"] <= CUTOFF, path
    assert proposal["assumptions"]["market"]["as_of"] <= CUTOFF


def test_missing_data_path_leaves_nulls_and_lists_them():
    w = world(spy_ok=False)                  # beta cannot be computed
    w.map.pop(next(u for u in w.map if "stooq.com" in u and "s=intc.us" in u and "i=d" in u))
    m = mp.propose_mandate("INTC", CUTOFF, fetch=w)
    # no INTC daily price on stooq and no yahoo fixture -> price missing
    assert m["assumptions"]["market"]["price"] is None
    assert "assumptions.market.price" in m["missing"]
    assert m["assumptions"]["wacc"] is None and "assumptions.wacc" in m["missing"]
    e = m["provenance"]["assumptions.wacc"]
    assert e["value"] is None and e["basis"].startswith("not available — enter manually")
    assert mandate_check.schema_errors(m) == []   # a proposal may carry nulls


def test_unknown_ticker_is_an_error():
    with pytest.raises(mp.PrefillError):
        mp.propose_mandate("NOPE", CUTOFF, fetch=world())


def test_sic_peer_suggestion_documented_fallback():
    # ticker not in peer-sets.json -> same-SIC suggestion from EDGAR
    w = world(peers=("AMD", "NVDA"))
    sub = "https://data.sec.gov/submissions/CIK0000050863.json"
    html = ("<a href='?action=getcompany&CIK=0000002488'>x</a> "
            "<a href='?action=getcompany&CIK=0001045810'>y</a> "
            "<a href='?action=getcompany&CIK=0000050863'>self</a>")
    w.map[sub] = json.dumps({"sic": "3674"}).encode()
    w.map["https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&SIC=3674"
          "&owner=include&match=&count=100&hidefilings=0"] = html.encode()
    m = mp.propose_mandate("INTC", CUTOFF, fetch=w, references_dir=Path("/nonexistent"))
    assert [r["ticker"] for r in m["assumptions"]["peer_block"]["peers"]] == ["AMD", "NVDA"]
    e = m["provenance"]["assumptions.peer_block.peers"]
    assert e["source"] == "sec:sic-3674" and "coarse" in e["basis"]


# ---------------------------------------------------------- review and accept

def test_review_table_lists_fields(proposal):
    t = mp.review_table(proposal)
    assert t.splitlines()[0].split()[:3] == ["field", "value", "source"]
    assert "assumptions.wacc" in t and "assumptions.market.price" in t and "status: proposed" in t


def test_accept_records_edits_and_keeps_provenance(proposal):
    final = mp.accept_mandate(proposal, ["assumptions.exit_multiple_ebitda=15",
                                         "assumptions.forecast_bounds.segments.CCG=[-0.2, 0.3]"],
                              now=lambda: "2026-11-01T00:00:00Z")
    assert final["status"] == "accepted"
    assert final["approval"]["by"] == "human" and final["approval"]["at"] == "2026-11-01T00:00:00Z"
    edits = {e["path"]: e for e in final["approval"]["edits"]}
    ex = edits["assumptions.exit_multiple_ebitda"]
    assert ex["final"] == 15 and ex["proposed"] == proposal["assumptions"]["exit_multiple_ebitda"]
    assert edits["assumptions.forecast_bounds.segments.CCG"]["final"] == [-0.2, 0.3]
    assert set(edits) == {"assumptions.exit_multiple_ebitda",
                          "assumptions.forecast_bounds.segments.CCG"}
    p = final["provenance"]["assumptions.exit_multiple_ebitda"]
    assert p["edited"] is True and p["final"] == 15 and p["source"] == "derived"
    assert mandate_check.validate_mandate_full(final) == []


def test_accept_without_edits_has_empty_edit_list(proposal):
    assert mp.accept_mandate(proposal)["approval"]["edits"] == []


def test_editing_wacc_regenerates_the_grid_and_records_it(proposal):
    final = mp.accept_mandate(proposal, ["assumptions.wacc=0.09"])
    assert final["assumptions"]["sensitivity"]["wacc_points"][2] == 0.09
    assert any(e["path"].startswith("assumptions.sensitivity") for e in final["approval"]["edits"])


def test_accept_refuses_while_fields_are_missing():
    w = world(spy_ok=False)
    m = mp.propose_mandate("INTC", CUTOFF, fetch=w)
    with pytest.raises(mp.PrefillError, match="assumptions.wacc"):
        mp.accept_mandate(m)
    ok = mp.accept_mandate(m, ["assumptions.wacc=0.1"])
    assert ok["assumptions"]["wacc"] == 0.1 and ok["missing"] == []


def test_accept_validates_edits_and_live_rules(proposal):
    with pytest.raises(mp.PrefillError, match="terminal_growth"):
        mp.accept_mandate(proposal, ["assumptions.terminal_growth=0.5"])
    with pytest.raises(mp.PrefillError, match="after the cutoff"):
        mp.accept_mandate(proposal, ["assumptions.market.as_of=\"2026-11-05\""])
    with pytest.raises(mp.PrefillError, match="--set may only edit"):
        mp.accept_mandate(proposal, ["status=accepted"])
    with pytest.raises(mp.PrefillError):
        mp.accept_mandate(mp.accept_mandate(proposal))       # not a proposal any more


def test_parse_set_json_or_string():
    assert mp.parse_set("a.b=1.5") == ("a.b", 1.5)
    assert mp.parse_set("a.b=[1,2]") == ("a.b", [1, 2])
    assert mp.parse_set("a.b=hello") == ("a.b", "hello")
    with pytest.raises(mp.PrefillError):
        mp.parse_set("nonsense")


def test_draft_location(tmp_path):
    assert mp.draft_location(tmp_path, "INTC", CUTOFF) == tmp_path / "drafts" / f"intc-{CUTOFF}"
    case = tmp_path / "cases" / "intc-fy2026q3-20261030"
    case.mkdir(parents=True)
    (case / "state.json").write_text("{}")
    assert mp.draft_location(tmp_path, "INTC", CUTOFF) == case
    f = case / "mandate.proposed.json"
    (case / "mandate.json").write_text("{}")
    assert mp.accepted_path(f).name == "mandate.accepted.json"
