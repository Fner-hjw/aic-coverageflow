"""references/mandate-schema.json is the one definition of a mandate."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from coverageflow import cli, mandate_check

ROOT = Path(__file__).resolve().parent.parent


def _live():
    m = cli.default_mandate("intc-fy2026q3-20261030", "INTC", "2026-10-30")
    m["assumptions"]["market"]["price"] = 24.5
    m["assumptions"]["peer_block"]["peers"] = [{"ticker": "AMD", "ev_ebitda": 20.0,
                                                "p_e_ttm": None, "p_s": 6.0}]
    return m


def test_schema_file_is_valid_json_with_draft_2020_12_id():
    s = json.loads((ROOT / "references" / "mandate-schema.json").read_text())
    assert s["$schema"].endswith("2020-12/schema")


def test_default_and_live_mandates_pass():
    assert mandate_check.schema_errors(
        cli.default_mandate("x-fy2026q3-20261030", "INTC", "2026-10-30")) == []
    assert mandate_check.validate_mandate_full(_live()) == []


@pytest.mark.parametrize("path", sorted(ROOT.glob("examples/*/cases/*/mandate.json")))
def test_shipped_example_mandates_pass(path):
    assert mandate_check.schema_errors(json.loads(path.read_text())) == []


def test_newer_keys_are_described():
    m = _live()
    a = m["assumptions"]
    a["terminal_gate_ratio_max"] = 4.0
    a["forecast_bounds"].update(operating_margin_floor=0.0, operating_margin_ceiling=0.4,
                                capex_pct_bounds=[0.05, 0.3])
    m["limits"].update(stall_rounds=2, case_cost_usd_max=5.0, call_cost_usd_max=1.0,
                       cost_usd_max=1.0, role_timeouts={"writer": 600},
                       role_efforts={"extractor": "low", "writer": None})
    assert mandate_check.schema_errors(m) == []


@pytest.mark.parametrize("mutate,needle", [
    (lambda m: m["assumptions"].update(wacc="high"), "assumptions.wacc"),
    (lambda m: m["assumptions"].update(wacc=None), "assumptions.wacc"),
    (lambda m: m["assumptions"].pop("tax_rate"), "tax_rate: required"),
    (lambda m: m["assumptions"]["forecast_bounds"]["segments"].update(X=[1]),
     "segments.X"),
    (lambda m: m["assumptions"]["peer_block"]["peers"].append({"p_s": 1}),
     "ticker: required"),
    (lambda m: m.update(cutoff_date="30/10/2026"), "cutoff_date"),
    (lambda m: m["limits"].update(rounds_max=0), "rounds_max"),
    (lambda m: m.update(status="maybe"), "status"),
    (lambda m: m.update(status="accepted"), "approval"),
])
def test_schema_rejects(mutate, needle):
    m = _live()
    mutate(m)
    assert any(needle in e for e in mandate_check.schema_errors(m))


def test_proposed_mandate_may_carry_nulls_but_accepted_may_not():
    m = _live()
    m["assumptions"]["wacc"] = None
    m["status"] = "proposed"
    assert mandate_check.schema_errors(m) == []
    m["status"] = "accepted"
    m["approval"] = {"by": "human", "at": "t", "edits": []}
    assert any("wacc" in e for e in mandate_check.schema_errors(m))


def test_wizard_validation_runs_the_schema_too(monkeypatch):
    from ui import service
    payload = {
        "ticker": "INTC", "cutoff_date": "2026-10-30", "trigger_document": "d",
        "report_policy": {"length_words": [800, 1600], "sections": ["Summary"]},
        "assumptions": {"wacc": 0.1, "terminal_growth": 0.025,
                        "exit_multiple_ebitda": 14,
                        "forecast_bounds": {"segments": {"A": [0, 0.1]},
                                            "margin_floor_gross": 0.3,
                                            "margin_ceiling_gross": 0.5},
                        "peer_block": {"peers": [{"ticker": "AMD", "p_s": "x"}]}}}
    assert any("p_s" in e for e in service.validate_mandate(payload))
    payload["assumptions"]["peer_block"]["peers"][0]["p_s"] = 3.0
    assert service.validate_mandate(payload) == []


def test_unaccepted_gate():
    m = _live()
    mandate_check.refuse_unaccepted(m)
    m["status"] = "proposed"
    with pytest.raises(mandate_check.UnacceptedMandateError):
        mandate_check.refuse_unaccepted(m)
