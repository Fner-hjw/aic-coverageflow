"""prior_note / prior_valuation handed to the writer, prior.* evidence keys."""

from __future__ import annotations

import json

import pytest

import casekit
from coverageflow import _dispatch_test_stub as _stub
from coverageflow import ledger, render_note, statestore
from tests.test_orchestrator import (_critic_findings, _writer_output,
                                     build_env, load_state)
from tests.test_owners import _events, _finding, _packet  # noqa
from tests.test_publish_clean import orchmod_arts


# ----------------------------------------------------------------- prior note
def _seed_prior(tmp_path, ticker="INTC", cutoff="2026-07-31", with_clean=False):
    prior = tmp_path / "cases" / "intc-fy2026q2-20260731"
    (prior / "report").mkdir(parents=True)
    (prior / "rounds" / "round-001" / "generator").mkdir(parents=True)
    statestore.atomic_write_json(prior / "mandate.json",
                                 {"ticker": ticker, "cutoff_date": cutoff})
    statestore.atomic_write_json(prior / "state.json",
                                 {"case_id": prior.name, "candidate": {"built_round": 1}})
    statestore.atomic_write_json(
        prior / "rounds" / "round-001" / "generator" / "valuation.json",
        {"dcf": {"target_price": 31.5, "rating": "Hold", "upside": 0.05, "wacc": 0.104,
                 "years": [{"year": 1, "revenue": 55000.0, "ebit": 4200.0}],
                 "base": {"revenue": 52000.0}}})
    (prior / "report" / "v1-abc12345.md").write_text(
        "## Summary\nTarget 31.5 [EV:dcf.target_price].\n", encoding="utf-8")
    if with_clean:
        (prior / "report" / "v1-abc12345.clean.md").write_text(
            "## Summary\nClean prior text.\n", encoding="utf-8")
    return prior


class TestPriorNote:
    def test_no_prior_publication_sends_nothing(self, tmp_path):
        orch, dispatch, clk, case_dir = build_env(tmp_path)
        orch.run()
        payload = dispatch.calls_of("writer")[0].payload
        assert "prior_note" not in payload and "prior_valuation" not in payload
        assert not any(k.startswith("prior.") for k in orch._evidence_index(orchmod_arts(1)))

    def test_a_prior_note_and_valuation_reach_the_writer(self, tmp_path):
        orch, dispatch, clk, case_dir = build_env(tmp_path)
        _seed_prior(tmp_path)
        orch.run()
        payload = dispatch.calls_of("writer")[0].payload
        assert "Target 31.5" in payload["prior_note"]
        assert "[EV:" not in payload["prior_note"]          # reader text, no markers
        pv = payload["prior_valuation"]
        assert pv["target_price"] == 31.5 and pv["rating"] == "Hold"
        assert pv["estimates"]["year1_revenue"] == 55000.0
        assert pv["source_case"] == "intc-fy2026q2-20260731"
        # other roles are not handed the prior note
        assert "prior_note" not in dispatch.calls_of("model")[0].payload

    def test_prior_values_are_citable_evidence_keys(self, tmp_path):
        orch, dispatch, clk, case_dir = build_env(tmp_path)
        _seed_prior(tmp_path)
        orch.run()
        idx = orch._evidence_index(orchmod_arts(1))
        assert idx["prior.target_price"] == "prior/prior-valuation.json"
        assert "prior.estimates.year1_revenue" in idx
        manifest = statestore.read_json(
            case_dir / "rounds" / "round-001" / "generator" / "candidate-manifest.json")
        assert "prior.target_price" in manifest["evidence_index"]
        frozen = statestore.read_json(case_dir / "prior" / "prior-valuation.json")
        assert frozen["present"] and frozen["valuation"]["target_price"] == 31.5

    def test_the_published_clean_note_is_preferred(self, tmp_path):
        orch, dispatch, clk, case_dir = build_env(tmp_path)
        _seed_prior(tmp_path, with_clean=True)
        orch.run()
        assert "Clean prior text" in dispatch.calls_of("writer")[0].payload["prior_note"]

    def test_other_tickers_and_later_cutoffs_are_ignored(self, tmp_path):
        orch, dispatch, clk, case_dir = build_env(tmp_path)
        _seed_prior(tmp_path, ticker="AMD")
        later = _seed_prior(tmp_path / "x", cutoff="2026-12-31")   # not in this workspace
        orch.run()
        assert "prior_note" not in dispatch.calls_of("writer")[0].payload

    def test_the_lookup_is_frozen_for_the_case(self, tmp_path):
        orch, dispatch, clk, case_dir = build_env(tmp_path)
        assert orch._prior_publication() is None
        _seed_prior(tmp_path)
        orch._prior_cache = None
        assert orch._prior_publication() is None        # sentinel on disk wins
