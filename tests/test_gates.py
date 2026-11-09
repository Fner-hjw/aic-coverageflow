"""Tests for gates.py — the LLM-free numerical/structural gate set (stream A2).

All gates are deterministic: same inputs -> same outcomes. Engine calls are
injected stub doubles with the frozen signatures from
docs/contracts/interfaces.md section 4.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import casekit
from coverageflow import gates

SCHEMA = Path(__file__).parent / "fixtures" / "minimal-model-schema.json"


def stub_build_model(facts, mapping_version: str):
    """Stub double of model_engine.build_model (deterministic, no I/O)."""
    series = {
        "income_statement.revenue": {"2026-Q3": 13500.0, "yoy": 0.049},
        "income_statement.gross_margin": {"2026-Q3": 0.42},
    }
    return {"artifact": "model", "engine": "stub-model@1.0",
            "mapping_tables": mapping_version, "series": series}


def stub_value_company(model, forecast, assumptions):
    """Stub double of valuation_engine.value_company."""
    return {"artifact": "valuation", "engine": "stub-val@1.0",
            "dcf": {"equity_value": 120000.0, "per_share": 28.0},
            "comps": {"per_share": 26.0}}


def make_engine(**kw) -> gates.GateEngine:
    kw.setdefault("schema_path", SCHEMA)
    kw.setdefault("build_model_fn", stub_build_model)
    kw.setdefault("value_fn", stub_value_company)
    return gates.GateEngine(**kw)


CITED_FACT = {
    "field": "income_statement.revenue", "period": "2026-Q3", "value": 13500.0,
    "unit": "USDmm", "citation": {
        "doc_id": "intc-10q-2026q3", "slice_id": "intc-10q-2026q3/IS",
        "anchor": "Consolidated Statements of Operations", "locator": "line 14"},
}

GOOD_FACTS = {"role": "extractor", "request_id": "req-0001",
              "candidate_id": "cand-1", "facts": [dict(CITED_FACT)]}

GOOD_FORECAST = {
    "role": "model", "horizon_years": 5,
    "segments": [
        {"segment": "CCG", "growth": [0.02, 0.02, 0.02, 0.02, 0.02],
         "drivers": [{"text": "segment table", "citation": {}}]},
        {"segment": "DCAI", "growth": [0.18, 0.16, 0.14, 0.12, 0.10],
         "drivers": [{"text": "segment table", "citation": {}}]},
    ],
    "margin_glide": {"gross": [0.40, 0.41, 0.42, 0.42, 0.43]},
    "terminal_basis": "mandate", "conformance": "schema 1.0", "prompt_version": "1.0",
}


# ===========================================================================
# cutoff discipline
# ===========================================================================

class TestCutoffGate:
    def test_all_docs_at_or_before_cutoff_pass(self):
        ok, findings = gates.gate_manifest_cutoff(casekit.MANIFEST, "2026-10-30")
        assert ok and findings == []

    def test_post_cutoff_doc_rejected_with_doc_id(self):
        manifest = {
            "as_of_cutoff": "2026-10-30",
            "documents": [
                dict(casekit.MANIFEST["documents"][0]),
                dict(casekit.MANIFEST["documents"][0],
                     doc_id="intc-10q-2026q4", filed="2026-11-15"),
            ],
        }
        ok, findings = gates.gate_manifest_cutoff(manifest, "2026-10-30")
        assert not ok
        assert any("intc-10q-2026q4" in f["summary"] for f in findings)
        assert findings[0]["affected_artifacts"] == ["inputs/manifest.json"]

    def test_exact_cutoff_date_passes(self):
        ok, _ = gates.gate_manifest_cutoff(casekit.MANIFEST, "2026-10-30")
        assert ok  # filed == cutoff is allowed (cutoff is inclusive)


# ===========================================================================
# facts schema + citations
# ===========================================================================

class TestFactsGates:
    def test_valid_facts_pass_schema(self):
        ok, findings = gates.gate_facts_schema(GOOD_FACTS, SCHEMA)
        assert ok, findings

    def test_fact_missing_citation_field_fails_schema(self):
        bad = {"role": "extractor", "request_id": "r", "candidate_id": "c",
               "facts": [{"field": "x", "period": "p", "value": 1.0,
                          "citation": {"doc_id": "d", "slice_id": "s",
                                       "anchor": "a"} }]}  # locator missing
        ok, findings = gates.gate_facts_schema(bad, SCHEMA)
        assert not ok
        assert any("locator" in f["summary"] for f in findings)

    def test_fact_with_extra_keys_still_allowed(self):
        import copy
        facts = copy.deepcopy(GOOD_FACTS)
        facts["notes"] = ["something"]
        facts["facts"][0]["confidence"] = 0.98
        ok, _ = gates.gate_facts_schema(facts, SCHEMA)
        assert ok

    def test_missing_facts_array_fails(self):
        ok, findings = gates.gate_facts_schema({"role": "extractor"}, SCHEMA)
        assert not ok
        assert any("facts" in f["summary"] for f in findings)

    def test_uncited_fact_flagged(self):
        import copy
        facts = copy.deepcopy(GOOD_FACTS)
        facts["facts"].append({"field": "income_statement.ebitda",
                               "period": "2026-Q3", "value": 1000.0})
        ok, findings = gates.gate_citation_presence(facts)
        assert not ok
        assert any("income_statement.ebitda" in f["summary"] for f in findings)

    def test_citation_to_unknown_doc_flagged(self):
        import copy
        facts = copy.deepcopy(GOOD_FACTS)
        facts["facts"][0]["citation"]["doc_id"] = "bogus-doc"
        ok, findings = gates.gate_citation_presence(facts, manifest=casekit.MANIFEST)
        assert not ok
        assert any("bogus-doc" in f["summary"] for f in findings)

    def test_cited_facts_pass(self):
        ok, findings = gates.gate_citation_presence(GOOD_FACTS, manifest=casekit.MANIFEST)
        assert ok, findings


# ===========================================================================
# forecast bounds vs mandate
# ===========================================================================

class TestForecastBoundsGate:
    BOUNDS = casekit.MANDATE["assumptions"]["forecast_bounds"]

    def test_in_bounds_forecast_passes(self):
        ok, findings = gates.gate_forecast_bounds(GOOD_FORECAST, self.BOUNDS)
        assert ok, findings

    def test_growth_above_segment_max_flagged_with_index(self):
        import copy
        fc = copy.deepcopy(GOOD_FORECAST)
        fc["segments"][0]["growth"][2] = 0.50  # CCG max 0.12
        ok, findings = gates.gate_forecast_bounds(fc, self.BOUNDS)
        assert not ok
        f = findings[0]
        assert "CCG" in f["summary"] and "year 3" in f["summary"]
        assert f["affected_artifacts"] == ["forecast_assumptions.json"]

    def test_growth_below_segment_min_flagged(self):
        import copy
        fc = copy.deepcopy(GOOD_FORECAST)
        fc["segments"][1]["growth"][0] = -0.20  # DCAI min -0.05
        ok, findings = gates.gate_forecast_bounds(fc, self.BOUNDS)
        assert not ok
        assert "DCAI" in findings[0]["summary"]

    def test_margin_below_floor_flagged(self):
        import copy
        fc = copy.deepcopy(GOOD_FORECAST)
        fc["margin_glide"]["gross"][4] = 0.25  # floor 0.30
        ok, findings = gates.gate_forecast_bounds(fc, self.BOUNDS)
        assert not ok
        assert any("margin" in f["summary"] for f in findings)

    def test_margin_above_ceiling_flagged(self):
        import copy
        fc = copy.deepcopy(GOOD_FORECAST)
        fc["margin_glide"]["gross"][0] = 0.60  # ceiling 0.55
        ok, findings = gates.gate_forecast_bounds(fc, self.BOUNDS)
        assert not ok
        assert any("ceiling" in f["summary"] for f in findings)

    def test_unknown_segment_and_horizon_mismatch_flagged(self):
        import copy
        fc = copy.deepcopy(GOOD_FORECAST)
        fc["segments"].append({"segment": "Mythical", "growth": [0.1] * 5})
        fc["segments"][0]["growth"] = [0.1] * 3  # wrong horizon length
        ok, findings = gates.gate_forecast_bounds(fc, self.BOUNDS)
        assert not ok
        assert any("Mythical" in f["summary"] for f in findings)
        assert any("CCG" in f["summary"] and "horizon" in f["summary"] for f in findings)


# ===========================================================================
# report evidence-link rule
# ===========================================================================

class TestReportEvidenceGate:
    def test_fully_linked_report_passes(self):
        md = ("Revenue rose to 13,500 [EV:income_statement.revenue] with gross margin "
              "of 0.42 [EV:income_statement.gross_margin].\nOur DCF yields 28.0 "
              "[EV:dcf.equity_per_share] per share.")
        ok, findings = gates.gate_report_evidence_links(md)
        assert ok, findings

    def test_bare_number_without_marker_flagged(self):
        md = "Revenue rose to 13,500 in the quarter."
        ok, findings = gates.gate_report_evidence_links(md)
        assert not ok
        assert any("13,500" in f["summary"] for f in findings)
        assert findings[0]["affected_artifacts"] == ["report.md"]

    def test_marker_after_next_number_does_not_cover_it(self):
        md = "Revenue was 13,500 and EBITDA 4,000 [EV:x]."
        ok, findings = gates.gate_report_evidence_links(md)
        assert not ok
        assert any("13,500" in f["summary"] for f in findings)

    def test_line_with_no_numbers_passes_trivially(self):
        ok, findings = gates.gate_report_evidence_links("Qualitative commentary only.")
        assert ok and findings == []

    def test_tokens_glued_to_letters_exempt(self):
        md = "Segment reporting uses FY26 conventions and Q3 results [EV:period]."
        ok, findings = gates.gate_report_evidence_links(md)
        assert ok, findings  # FY26 / Q3 are not standalone numeric claims

    def test_ev_marker_keys_validated_against_evidence_index(self):
        md = "Revenue rose to 13,500 [EV:nonexistent_key]."
        ok, findings = gates.gate_report_evidence_links(md, evidence_index={"income_statement.revenue": "facts.json"})
        assert not ok
        assert any("nonexistent_key" in f["summary"] for f in findings)

    def test_unknown_marker_key_ok_without_index(self):
        md = "Revenue rose to 13,500 [EV:whatever]."
        ok, findings = gates.gate_report_evidence_links(md)
        assert ok, findings


# ===========================================================================
# recomputation gate (injected engine doubles)
# ===========================================================================

class TestRecomputeGate:
    MODEL_JSON = {
        "artifact": "model", "engine": "stub-model@1.0", "mapping_tables": "1.0",
        "series": {"income_statement.revenue": {"2026-Q3": 13500.0, "yoy": 0.049},
                   "income_statement.gross_margin": {"2026-Q3": 0.42}},
    }
    VALUATION_JSON = {
        "artifact": "valuation", "engine": "stub-val@1.0",
        "dcf": {"equity_value": 120000.0, "per_share": 28.0},
        "comps": {"per_share": 26.0},
    }

    def test_recompute_matches(self):
        eng = make_engine()
        report = eng.run_recompute(GOOD_FACTS, GOOD_FORECAST, casekit.MANDATE,
                                   self.MODEL_JSON, self.VALUATION_JSON)
        assert report.passed, report.finding_dicts

    def test_recompute_detects_tampered_series_value(self):
        import copy
        model = copy.deepcopy(self.MODEL_JSON)
        model["series"]["income_statement.revenue"]["2026-Q3"] = 999.0
        eng = make_engine()
        rep = eng.run_recompute(GOOD_FACTS, GOOD_FORECAST, casekit.MANDATE, model, self.VALUATION_JSON)
        assert not rep.passed
        assert any("income_statement.revenue" in f["summary"] for f in rep.finding_dicts)

    def test_recompute_detects_per_share_disagreement(self):
        import copy
        val = copy.deepcopy(self.VALUATION_JSON)
        val["dcf"]["per_share"] = 50.0
        eng = make_engine()
        rep = eng.run_recompute(GOOD_FACTS, GOOD_FORECAST, casekit.MANDATE, self.MODEL_JSON, val)
        assert not rep.passed
        assert any("per_share" in f["summary"] for f in rep.finding_dicts)

    def test_engine_exception_becomes_finding_not_crash(self):
        def boom(facts, mapping_version):
            raise RuntimeError("engine fault")
        eng = gates.GateEngine(schema_path=SCHEMA, build_model_fn=boom,
                               value_fn=stub_value_company)
        rep = eng.run_recompute(GOOD_FACTS, GOOD_FORECAST, casekit.MANDATE,
                                self.MODEL_JSON, self.VALUATION_JSON)
        assert not rep.passed
        assert any("engine fault" in f["summary"] for f in rep.finding_dicts)


# ===========================================================================
# candidate-manifest integrity + combined run
# ===========================================================================

class TestManifestIntegrityAndPipeline:
    def _materials(self, tmp_path: Path):
        dir_ = tmp_path / "round-001" / "generator"
        dir_.mkdir(parents=True)
        facts_path = dir_ / "facts.json"
        import json as _json
        facts_path.write_text(_json.dumps(GOOD_FACTS))
        from coverageflow import statestore
        manifest = {
            "request_id": "req-0001", "candidate_id": "cand-1", "round": 1,
            "reports": [
                {"role": "extractor",
                 "artifact": str(facts_path.relative_to(tmp_path)),
                 "sha256": statestore.sha256_file(facts_path)},
            ],
            "evidence_index": {"income_statement.revenue": "facts.json"},
            "scope_statement": "full rebuild",
        }
        return manifest

    def test_manifest_hashes_match_pass(self, tmp_path):
        manifest = self._materials(tmp_path)
        ok, findings = gates.gate_candidate_manifest_integrity(manifest, case_dir=tmp_path)
        assert ok, findings

    def test_manifest_hash_mismatch_flagged(self, tmp_path):
        manifest = self._materials(tmp_path)
        manifest["reports"][0]["sha256"] = "0" * 64
        ok, findings = gates.gate_candidate_manifest_integrity(manifest, case_dir=tmp_path)
        assert not ok
        assert any("sha256" in f["summary"].lower() or "mismatch" in f["summary"].lower()
                   for f in findings)

    def test_run_all_passes_on_clean_candidate(self, tmp_path):
        manifest = self._materials(tmp_path)
        ctx = gates.CandidateContext(
            facts=GOOD_FACTS,
            forecast=GOOD_FORECAST,
            report_md="Revenue rose to 13,500 [EV:income_statement.revenue].",
            candidate_manifest=manifest,
            input_manifest=casekit.MANIFEST,
            mandate=casekit.MANDATE,
            model_json=TestRecomputeGate.MODEL_JSON,
            valuation_json=TestRecomputeGate.VALUATION_JSON,
            case_dir=tmp_path,
        )
        eng = make_engine()
        report = eng.run_all(ctx)
        assert report.passed, [r for r in report.results if not r.ok]
        assert [r.gate for r in report.results] == list(gates.GATE_ORDER)

    def test_run_all_collects_findings_from_failing_gates(self, tmp_path):
        manifest = self._materials(tmp_path)
        ctx = gates.CandidateContext(
            facts=GOOD_FACTS,
            forecast=GOOD_FORECAST,
            report_md="Revenue rose to 13,500.",  # unlinked number
            candidate_manifest=manifest,
            input_manifest=casekit.MANIFEST,
            mandate=casekit.MANDATE,
            model_json=TestRecomputeGate.MODEL_JSON,
            valuation_json=TestRecomputeGate.VALUATION_JSON,
            case_dir=tmp_path,
        )
        eng = make_engine()
        report = eng.run_all(ctx)
        assert not report.passed
        assert any(f["gate"] == "report_evidence_links" for f in report.finding_dicts)
        led_friendly = report.ledger_findings()
        assert led_friendly and all("dedup_key" in f for f in led_friendly)

    def test_schema_missing_file_fails_closed(self, tmp_path):
        eng = gates.GateEngine(schema_path=tmp_path / "nope.json")
        ok, findings = eng._schema_findings({"role": "extractor", "facts": []})
        assert not ok
        assert any("schema" in f["summary"].lower() for f in findings)
