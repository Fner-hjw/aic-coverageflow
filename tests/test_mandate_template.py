"""Mandate template: valuation-engine required keys in every mandate helper.

Integration stream (A6), Task 4. Anywhere a mandate is CREATED in code helpers
it must include the human-owned valuation inputs the A1 valuation engine reads
(coverageflow.valuation_engine.Assumptions.from_payload):

    assumptions.market.price                     (DCF/comps upside vs market)
    assumptions.own_band.{ev_ebitda,p_e,p_s}     (own historical multiple band)
    assumptions.sensitivity.{wacc_points,terminal_growth_points} (grid)

The frozen fixtures (tests/fixtures/casekit.py, docs/contracts) are untouched;
the engine already defaults missing keys - the new keys make produced mandates
self-contained per design-decisions section 4.
"""

from __future__ import annotations

import pytest

from coverageflow import cli
from coverageflow.valuation_engine import Assumptions


class TestCliDefaultMandate:
    def test_includes_valuation_required_blocks(self):
        m = cli.default_mandate("intc-fy2026q3-20261030", "INTC", "2026-10-30")
        a = m["assumptions"]
        assert a["market"]["price"] is None
        assert len(a["own_band"]["ev_ebitda"]) == 2
        assert len(a["own_band"]["p_e"]) == 2
        assert len(a["own_band"]["p_s"]) == 2
        assert len(a["sensitivity"]["wacc_points"]) == 5
        assert len(a["sensitivity"]["terminal_growth_points"]) == 5

    def test_feeds_the_valuation_engine_directly(self):
        m = cli.default_mandate("intc-fy2026q3-20261030", "INTC", "2026-10-30")
        asm = Assumptions.from_payload(m["assumptions"])
        assert asm.market_price is None
        assert asm.own_band["p_e"] == [12.0, 26.0]
        assert asm.sensitivity_points[0] == m["assumptions"]["sensitivity"][
            "wacc_points"]
        assert asm.sensitivity_points[1] == m["assumptions"]["sensitivity"][
            "terminal_growth_points"]

    def test_sensitivity_grid_is_centered_and_explicit(self):
        m = cli.default_mandate("x-latest-20261030", "INTC", "2026-10-30")
        sens = m["assumptions"]["sensitivity"]
        wacc = m["assumptions"]["wacc"]
        g = m["assumptions"]["terminal_growth"]
        assert sens["wacc_points"][2] == wacc
        assert sens["terminal_growth_points"][2] == g
        assert sens["wacc_points"] == sorted(sens["wacc_points"])
        assert sens["terminal_growth_points"] == sorted(
            sens["terminal_growth_points"])
        assert all(t < wacc for t in sens["terminal_growth_points"])


class TestUiMandateBuilder:
    @pytest.fixture()
    def service(self):
        from ui import service
        return service

    def test_wizard_payload_mandate_carries_the_blocks(self, service):
        mandate = service._mandate_from_payload(
            {"ticker": "INTC", "cutoff_date": "2026-10-30",
             "trigger_document": "intc-10q-2026q3",
             "assumptions": {"wacc": 0.10, "terminal_growth": 0.02}},
            "man-0001", "intc-fy2026q3-20261030")
        a = mandate["assumptions"]
        assert {"market", "own_band", "sensitivity"} <= set(a)
        asm = Assumptions.from_payload(a)
        assert asm.market_price is None
        assert len(asm.sensitivity_points[0]) == 5

    def test_wizard_can_supply_market_price_and_band(self, service):
        mandate = service._mandate_from_payload(
            {"ticker": "INTC", "cutoff_date": "2026-10-30",
             "trigger_document": "intc-10q-2026q3",
             "assumptions": {
                 "wacc": 0.10, "terminal_growth": 0.02,
                 "market": {"as_of": "2026-10-30", "price": 24.50},
                 "own_band": {"ev_ebitda": [11.0, 17.0], "p_e": [13.0, 27.0],
                              "p_s": [3.0, 8.0]}}},
            "man-0002", "intc-fy2026q3-20261030")
        asm = Assumptions.from_payload(mandate["assumptions"])
        assert asm.market_price == 24.50
        assert asm.own_band["ev_ebitda"] == [11.0, 17.0]


class TestEngineStillDefaultsAbsentBlocks:
    """Frozen fixtures (casekit) keep working without the new keys."""

    def test_casekit_mandate_still_parses(self):
        import casekit  # tests/fixtures on sys.path via conftest
        asm = Assumptions.from_payload(casekit.MANDATE["assumptions"])
        assert asm.own_band is None
        assert asm.market_price is None
        assert len(asm.sensitivity_points[0]) == 5  # engine default grid
