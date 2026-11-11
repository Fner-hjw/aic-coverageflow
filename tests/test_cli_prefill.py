"""case prefill / review / accept and the 'nothing runs on a proposal' rule."""

from __future__ import annotations

import json

import pytest

from coverageflow import cli, market_data, mandate_check, statestore
from tests.market_fixtures import CUTOFF, world


@pytest.fixture()
def ws(tmp_path, monkeypatch):
    fetch = world()
    monkeypatch.setattr(market_data, "default_fetch", lambda *a, **k: fetch)
    return tmp_path


def run(ws, *argv):
    return cli.main(["--workspace", str(ws), *argv])


def test_prefill_writes_draft_and_prints_table(ws, capsys):
    assert run(ws, "case", "prefill", "INTC", "--cutoff", CUTOFF) == 0
    out = capsys.readouterr().out
    draft = ws / "drafts" / f"intc-{CUTOFF}" / "mandate.proposed.json"
    assert draft.is_file() and "assumptions.wacc" in out and "mandate.proposed.json" in out
    assert json.loads(draft.read_text())["status"] == "proposed"
    assert run(ws, "case", "review", f"intc-{CUTOFF}") == 0
    assert "assumptions.market.price" in capsys.readouterr().out


def test_prefill_options_peers_and_erp(ws):
    run(ws, "case", "prefill", "INTC", "--cutoff", CUTOFF, "--peers", "AMD", "--erp", "0.06")
    m = json.loads((ws / "drafts" / f"intc-{CUTOFF}" / "mandate.proposed.json").read_text())
    assert [p["ticker"] for p in m["assumptions"]["peer_block"]["peers"]] == ["AMD"]
    assert m["provenance"]["assumptions.wacc"]["inputs"]["equity_risk_premium"]["value"] == 0.06


def test_prefill_goes_to_the_case_dir_when_the_case_exists(ws):
    case = ws / "cases" / "intc-fy2026q3-20261030"
    case.mkdir(parents=True)
    (case / "state.json").write_text("{}")
    run(ws, "case", "prefill", "INTC", "--cutoff", CUTOFF)
    assert (case / "mandate.proposed.json").is_file()


def test_accept_with_set_records_edits(ws, capsys):
    run(ws, "case", "prefill", "INTC", "--cutoff", CUTOFF)
    capsys.readouterr()
    d = ws / "drafts" / f"intc-{CUTOFF}"
    assert run(ws, "case", "accept", str(d), "--set", "assumptions.exit_multiple_ebitda=16",
               "--set", "assumptions.tax_rate=0.18") == 0
    final = json.loads((d / "mandate.json").read_text())
    assert final["status"] == "accepted" and final["approval"]["by"] == "human"
    assert {e["path"]: e["final"] for e in final["approval"]["edits"]} == {
        "assumptions.exit_multiple_ebitda": 16, "assumptions.tax_rate": 0.18}
    assert final["provenance"]["assumptions.tax_rate"]["edited"] is True
    assert (d / "mandate.proposed.json").is_file()       # the proposal is kept
    assert mandate_check.validate_mandate_full(final) == []


def test_accept_refuses_invalid_edit(ws):
    run(ws, "case", "prefill", "INTC", "--cutoff", CUTOFF)
    with pytest.raises(SystemExit, match="refused"):
        run(ws, "case", "accept", f"intc-{CUTOFF}", "--set", "assumptions.wacc=0.9")
    assert not (ws / "drafts" / f"intc-{CUTOFF}" / "mandate.json").exists()


def test_case_new_refuses_a_proposed_mandate(ws):
    run(ws, "case", "prefill", "INTC", "--cutoff", CUTOFF)
    proposed = ws / "drafts" / f"intc-{CUTOFF}" / "mandate.proposed.json"
    with pytest.raises(SystemExit, match="status: proposed"):
        run(ws, "case", "new", "--ticker", "INTC", "--trigger-doc", "intc-10q-2026q3",
            "--cutoff", CUTOFF, "--mandate", str(proposed))
    assert not (ws / "cases").exists()


def test_case_new_takes_an_accepted_mandate_and_run_refuses_a_proposed_one(ws, capsys):
    run(ws, "case", "prefill", "INTC", "--cutoff", CUTOFF)
    d = ws / "drafts" / f"intc-{CUTOFF}"
    run(ws, "case", "accept", str(d))
    capsys.readouterr()
    run(ws, "case", "new", "--ticker", "INTC", "--trigger-doc", "intc-10q-2026q3",
        "--cutoff", CUTOFF, "--mandate", str(d / "mandate.json"))
    case = ws / "cases" / "intc-fy2026q3-20261030"
    m = statestore.read_json(case / "mandate.json")
    assert m["status"] == "accepted" and m["case_id"] == case.name and m["provenance"]
    # a proposal planted in a case is refused by run and resume
    m["status"] = "proposed"
    (case / "mandate.json").write_text(json.dumps(m))
    for cmd in ("run", "resume"):
        with pytest.raises(SystemExit, match="machine proposal"):
            run(ws, cmd, "--case", case.name)
