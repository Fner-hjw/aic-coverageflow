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

    def test_marker_does_not_reach_back_across_a_clause_break(self):
        """A marker cites the clause it closes, not the previous sentence."""
        md = "Revenue was 13,500. EBITDA was 4,000 [EV:x]."
        ok, findings = gates.gate_report_evidence_links(md)
        assert not ok
        assert any("13,500" in f["summary"] for f in findings)
        assert not any("4,000" in f["summary"] for f in findings)

    def test_one_marker_cites_a_whole_band(self):
        """A cited range is one clause: the band and its implied values.

        Per-number markers made this unwritable -- "10.0x [EV:k] to 16.0x
        [EV:k]" -- so the writer could not cite a multiple band at all.
        """
        md = ("The own-band multiples are the only anchor: EV/EBITDA of 10.0x "
              "to 16.0x implies 27.97 to 44.31 USD [EV:comps.own_band].\n")
        ok, findings = gates.gate_report_evidence_links(
            md, evidence_index={"comps.own_band": "valuation.json"})
        assert ok, findings

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


# ===========================================================================
# regressions found by the backend/dispatch audit
# ===========================================================================

class TestAuditRegressions:
    def test_margin_glide_length_is_checked_against_the_horizon(self):
        import copy
        fc = copy.deepcopy(GOOD_FORECAST)
        fc["margin_glide"]["gross"] = [0.40, 0.41]  # horizon is 5
        ok, findings = gates.gate_forecast_bounds(
            fc, casekit.MANDATE["assumptions"]["forecast_bounds"])
        assert not ok
        assert any("margin_glide" in f["summary"] for f in findings)

    def test_empty_margin_glide_is_flagged(self):
        import copy
        fc = copy.deepcopy(GOOD_FORECAST)
        fc["margin_glide"]["gross"] = []
        ok, findings = gates.gate_forecast_bounds(
            fc, casekit.MANDATE["assumptions"]["forecast_bounds"])
        assert not ok
        assert any("margin_glide" in f["summary"] for f in findings)

    def test_missing_valuation_json_is_a_finding_not_a_silent_pass(self):
        eng = make_engine()
        rep = eng.run_recompute(GOOD_FACTS, GOOD_FORECAST, casekit.MANDATE,
                                TestRecomputeGate.MODEL_JSON, None)
        assert not rep.passed
        assert any("valuation.json" in f["summary"] for f in rep.finding_dicts)

    def test_comps_values_are_recomputed(self):
        import copy
        val = copy.deepcopy(TestRecomputeGate.VALUATION_JSON)
        val["comps"]["per_share"] = 99.0
        eng = make_engine()
        rep = eng.run_recompute(GOOD_FACTS, GOOD_FORECAST, casekit.MANDATE,
                                TestRecomputeGate.MODEL_JSON, val)
        assert not rep.passed
        assert any("comps" in f["summary"] for f in rep.finding_dicts)

    def test_form_names_and_dates_need_no_evidence_link(self):
        md = ("# INTC 10-Q, FY2026 Q3\n"
              "Filed 2026-10-30 for the quarter ended Q3 FY26.\n")
        ok, findings = gates.gate_report_evidence_links(md)
        assert ok, findings

    def test_two_identical_numbers_on_one_line_get_distinct_keys(self):
        ok, findings = gates.gate_report_evidence_links("Two numbers 7 and 7 again.\n")
        assert not ok
        keys = [f["dedup_key"] for f in findings]
        assert len(keys) == 2 and len(set(keys)) == 2

    def test_cutoff_gate_parses_dates_instead_of_comparing_strings(self):
        manifest = {"documents": [
            {"doc_id": "d1", "filed": "11/15/2026"},   # sorts before 2026-10-30
            {"doc_id": "d2", "filed": "2026-10-30"},
        ]}
        ok, findings = gates.gate_manifest_cutoff(manifest, "2026-10-30")
        assert not ok
        assert any("d1" in f["summary"] for f in findings)

    def test_cutoff_gate_fails_closed_on_a_missing_filed_date(self):
        manifest = {"documents": [{"doc_id": "d1"}]}
        ok, findings = gates.gate_manifest_cutoff(manifest, "2026-10-30")
        assert not ok
        assert any("filing date" in f["summary"] for f in findings)

    def test_cutoff_gate_fails_closed_on_an_unparsable_cutoff(self):
        ok, findings = gates.gate_manifest_cutoff({"documents": []}, "30/10/2026")
        assert not ok
        assert findings[0]["dedup_key"] == "cutoff:unparsable"

    def test_facts_schema_gate_refuses_a_field_catalogue(self, tmp_path):
        catalogue = tmp_path / "model-schema.json"
        catalogue.write_text('{"fields": [{"name": "revenue", "unit": "USDmm"}]}',
                             encoding="utf-8")
        ok, findings = gates.gate_facts_schema({"facts": []}, catalogue)
        assert not ok
        assert findings[0]["dedup_key"] == "schema:not-a-schema"

    def test_facts_schema_gate_accepts_a_real_schema(self):
        ok, findings = gates.gate_facts_schema(GOOD_FACTS, SCHEMA)
        assert ok, findings

    def test_schema_type_union_is_supported(self, tmp_path):
        schema = tmp_path / "s.json"
        schema.write_text(
            '{"type": "object", "required": ["a"],'
            ' "properties": {"a": {"type": ["string", "null"]}}}',
            encoding="utf-8")
        ok, findings = gates.gate_facts_schema({"a": None}, schema)
        assert ok, findings
        ok, findings = gates.gate_facts_schema({"a": 5}, schema)
        assert not ok


class TestEvidenceLinkLabels:
    """A year is a label, not a claim.

    Intel's note says "Q3 2026" and "fiscal 2025" throughout. Treating those
    as unlinked numeric claims produced ~100 findings per round and no report
    could ever converge.
    """

    def test_bare_year_needs_no_marker(self):
        md = "In 2026 the company reported revenue of 13,500 [EV:income_statement.revenue]."
        ok, findings = gates.gate_report_evidence_links(md)
        assert ok, findings

    def test_apostrophe_year_shorthand_needs_no_marker(self):
        md = "Q3'26 revenue was 13,500 [EV:income_statement.revenue]."
        ok, findings = gates.gate_report_evidence_links(md)
        assert ok, findings

    def test_a_real_number_still_needs_a_marker(self):
        md = "In 2026 revenue was 13,500."
        ok, findings = gates.gate_report_evidence_links(md)
        assert not ok
        assert any("13,500" in f["summary"] for f in findings)

    def test_lowercase_marker_is_accepted(self):
        md = "Revenue was 13,500 [ev:income_statement.revenue]."
        ok, findings = gates.gate_report_evidence_links(md)
        assert ok, findings

    def test_decorated_marker_resolves_to_its_field(self):
        """A marker carrying locator context still names a real evidence key."""
        md = ("Revenue was 13,500 "
              "[EV:intc-10q-2026q3/IS:income_statement.revenue:2026-Q3].")
        ok, findings = gates.gate_report_evidence_links(
            md, evidence_index={"income_statement.revenue": "facts.json"})
        assert ok, findings

    def test_decorated_marker_with_an_unknown_field_still_fails(self):
        md = "Revenue was 13,500 [EV:intc-10q-2026q3/IS:made.up.field:2026-Q3]."
        ok, findings = gates.gate_report_evidence_links(
            md, evidence_index={"income_statement.revenue": "facts.json"})
        assert not ok


def test_provenance_header_block_needs_no_markers():
    """The header the writer must emit is metadata, not claims.

    Without this the required header fails the gate on its own request id,
    bundle id and sha256, and no report can ever pass.
    """
    md = ("> **Cutoff:** 2026-10-30 · **Bundle:** intc-2026-10 / "
          "40fc08efbf985dfd0b842acc61c31e553fc08eb17f29887246b578464840bbc4\n"
          "> **Candidate:** cand-bb62cc42579d · **Request:** req-0676\n"
          "\n## Summary\n"
          "Revenue was 13,500 [EV:income_statement.revenue].\n")
    ok, findings = gates.gate_report_evidence_links(
        md, evidence_index={"income_statement.revenue": "facts.json"})
    assert ok, findings


def test_provenance_header_versions_are_metadata_not_claims():
    """The header's card/conformance versions identify the run, not the company.

    The writer is required to emit the header block; its ``1.2`` and ``1.0``
    are version labels, so demanding an evidence marker for them made every
    conforming report fail the gate.
    """
    md = ("# INTC — Q3 2026 Quarterly Update\n"
          "\n"
          "> **Cutoff:** 2026-10-30 · **Bundle:** intc-2026-10 / "
          "40fc08efbf985dfd0b842acc61c31e553fc08eb17f29887246b578464840bbc4\n"
          "> **Candidate:** cand-8b9f3b963817 · **Request:** req-0696\n"
          "> **Prompt card:** 1.2 · **Conformance:** schema 1.0\n"
          "\n## Summary\n"
          "Revenue was 13,500 [EV:income_statement.revenue].\n")
    ok, findings = gates.gate_report_evidence_links(
        md, evidence_index={"income_statement.revenue": "facts.json"})
    assert ok, findings


def test_header_exemption_does_not_reach_the_body():
    """Only the header block is metadata; a blockquote in the body is prose."""
    md = ("> **Cutoff:** 2026-10-30\n"
          "\n## Summary\n"
          "> Revenue was 13,500 in the quarter.\n")
    ok, findings = gates.gate_report_evidence_links(md)
    assert not ok
    assert any("13,500" in f["summary"] for f in findings)


def test_provenance_versions_in_body_are_labels_but_financial_numbers_are_not():
    md = ("The provenance block carries writer prompt 1.3 and conformance "
          "schema 1.0; revenue was 13,500.\n")
    ok, findings = gates.gate_report_evidence_links(md)
    assert not ok
    assert len(findings) == 1
    assert '"13,500"' in findings[0]["summary"]

    ok, findings = gates.gate_report_evidence_links(
        md.replace("13,500.", "13,500 [EV:income_statement.revenue]."),
        evidence_index={"income_statement.revenue": "facts.json"})
    assert ok, findings


def test_unknown_key_finding_names_the_keys_that_do_resolve():
    """A shortened key must come back with the real ones, not another guess."""
    md = "Terminal value is 144,315 [EV:dcf.terminal_value].\n"
    index = {"dcf.terminal.gordon.terminal_value": "valuation.json",
             "dcf.terminal.exit_multiple.terminal_value": "valuation.json"}
    ok, findings = gates.gate_report_evidence_links(md, evidence_index=index)
    assert not ok
    unknown = [f for f in findings if "unknown-ev" in f["dedup_key"]]
    assert len(unknown) == 1
    assert "dcf.terminal.gordon.terminal_value" in unknown[0]["summary"]
    assert "dcf.terminal.exit_multiple.terminal_value" in unknown[0]["required_evidence"]


def test_ordered_list_markers_are_labels_not_claims():
    """A numbered risk list is structure; its numbers assert nothing."""
    md = ("1. **Foundry economics.** Revenue of 6,697 USDmm "
          "[EV:segments.foundry.revenue].\n"
          "2. **Cash conversion.** OCF was 13,184 USDmm "
          "[EV:cash_flow.operating_cash_flow].\n")
    ok, findings = gates.gate_report_evidence_links(md)
    assert ok, findings


def test_identifiers_are_exempt_but_claims_are_not():
    md = "Request req-0676 for bundle intc-2026-10 reported 13,500.\n"
    ok, findings = gates.gate_report_evidence_links(md)
    assert not ok
    assert [f["summary"] for f in findings] == [
        'numeric token "13,500" on line 1 lacks an evidence marker '
        "before the next number"]


def test_marker_in_prose_with_no_number_is_not_a_citation():
    """A methodology line quoting the marker syntax has no claim to link.

    The writer copied the placeholder out of the gate's own finding text, and
    the gate then failed the round on the writer's echo of it. A line with no
    numeric token cannot carry an unknown evidence key.
    """
    md = ("Every numeric token in this note carries an [EV:key] marker "
          "resolving to a canonical field id.\n")
    ok, findings = gates.gate_report_evidence_links(
        md, evidence_index={"income_statement.revenue": "facts.json"})
    assert ok, findings


def test_placeholder_marker_beside_a_real_number_still_fails():
    """The exemption is for claim-free lines, not for the placeholder itself."""
    md = "Revenue was 13,500 [EV:key].\n"
    ok, findings = gates.gate_report_evidence_links(
        md, evidence_index={"income_statement.revenue": "facts.json"})
    assert not ok
    assert any("unknown-ev:key" in f["dedup_key"] for f in findings)


def test_finding_text_carries_no_copyable_marker():
    """The finding must not hand the writer a literal marker to paste back."""
    md = "In 2026 revenue was 13,500.\n"
    ok, findings = gates.gate_report_evidence_links(md)
    assert not ok
    for f in findings:
        assert "[EV:" not in f["summary"]
        assert "[EV:" not in f["required_evidence"]


def test_marker_key_may_contain_spaces():
    """As-reported segment ids carry spaces: `segments.Intel Foundry.revenue`.

    A marker the regex cannot see makes every number on the line look
    unlinked, which is a false positive that no rewrite can clear.
    """
    md = ("| Intel Foundry | 6,697 [EV:segments.Intel Foundry.revenue] "
          "| (1,853) [EV:segments.Intel Foundry.operating_income] |")
    ok, findings = gates.gate_report_evidence_links(
        md, evidence_index={"segments.Intel Foundry.revenue": "facts.json",
                            "segments.Intel Foundry.operating_income": "facts.json"})
    assert ok, findings


def test_harness_evidence_index_wins_over_the_lead_echo(tmp_path):
    """The lead echoes the evidence index into the manifest; an echo is not a
    source of truth. A truncated echo made every marker in a correct report
    look unknown, and the report could never pass."""
    manifest = TestManifestIntegrityAndPipeline()._materials(tmp_path)
    manifest["evidence_index"] = {}          # the lead's echo, truncated
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
        evidence_index={"income_statement.revenue": "facts.json"},
    )
    report = make_engine().run_all(ctx)
    links = next(r for r in report.results if r.gate == "report_evidence_links")
    assert links.ok, links.findings


def test_sum_to_100_is_arithmetic_not_a_claim():
    """"the shares do not sum to 100%" is arithmetic on the cited values.

    The writer states it to explain why segment shares exceed the total; there
    is nothing for an evidence marker to attach to. A bare "100%" elsewhere is
    still a measurement and still needs a marker.
    """
    from coverageflow.gates import gate_report_evidence_links

    ok, findings = gate_report_evidence_links(
        "CCG was 49.0% [EV:segments.ccg.revenue_share_of_total]; the shares "
        "are computed against consolidated revenue and do not sum to 100%.")
    assert ok, findings

    ok, findings = gate_report_evidence_links("Revenue grew 100% in the quarter.")
    assert not ok


class TestEvidenceKeyResolution:
    """A writer's marker must resolve when the leaf path is right.

    The gate told the writer "the index has sensitivity.grid" while rejecting
    `dcf.sensitivity.grid`, and rejected `derived.revenue_yoy` outright. Both
    are the same evidence under a different name, so the finding reopened every
    round and the repair loop could not converge.
    """

    IDX = {"income_statement.revenue": "facts.json",
           "sensitivity.grid": "valuation.json",
           "derived.revenue_yoy": "model.json"}

    def test_an_artifact_prefix_resolves_to_the_leaf(self):
        from coverageflow.gates import _evidence_key
        assert _evidence_key("dcf.sensitivity.grid", self.IDX) == "sensitivity.grid"

    def test_a_growth_alias_resolves(self):
        from coverageflow.gates import _evidence_key
        assert _evidence_key("derived.revenue_yoy", self.IDX) == "derived.revenue_yoy"

    def test_locator_decoration_still_resolves(self):
        from coverageflow.gates import _evidence_key
        assert _evidence_key("doc/IS:income_statement.revenue:2026-Q3",
                             self.IDX) == "income_statement.revenue"

    def test_an_invented_key_still_fails(self):
        from coverageflow.gates import _evidence_key
        assert _evidence_key("made.up.metric", self.IDX) is None

    def test_the_report_that_looped_now_passes(self):
        from coverageflow.gates import gate_report_evidence_links
        report = (
            "Revenue reached 16,513 USDmm [EV:income_statement.revenue], up "
            "20.9% [EV:derived.revenue_yoy].\n"
            "The path applies to FY2025 revenue of 52,853 USDmm "
            "[EV:income_statement.revenue]; year 1 is FY2026E.\n"
            "The grid is unset [EV:dcf.sensitivity.grid].\n"
        )
        ok, findings = gate_report_evidence_links(report, self.IDX)
        assert ok, [f["summary"] for f in findings]


class TestForecastColumnOrdinals:
    """"year 1 is FY2026E" numbers a column, it does not measure the company."""

    def test_year_ordinal_needs_no_marker(self):
        from coverageflow.gates import gate_report_evidence_links
        ok, findings = gate_report_evidence_links(
            "Year 1 is FY2026E and year 5 is FY2030E.\n", {})
        assert ok, [f["summary"] for f in findings]

    def test_a_bare_number_beside_it_is_still_a_claim(self):
        from coverageflow.gates import gate_report_evidence_links
        ok, findings = gate_report_evidence_links(
            "Year 1 revenue is 59,877 USDmm.\n", {})
        assert not ok
        assert any("59,877" in f["summary"] for f in findings)


class TestSpanLabels:
    """"9M FY2026" is a period label, the same kind as "Q3" and "FY2026"."""

    def test_nine_month_label_needs_no_marker(self):
        from coverageflow.gates import gate_report_evidence_links
        ok, findings = gate_report_evidence_links(
            "| USDmm | Q3 FY2026 | 9M FY2026 | 6M FY2025 |\n", {})
        assert ok, [f["summary"] for f in findings]

    def test_a_bare_nine_is_still_a_claim(self):
        from coverageflow.gates import gate_report_evidence_links
        ok, findings = gate_report_evidence_links("Revenue rose 9 percent.\n", {})
        assert not ok


# ===========================================================================
# period coverage
# ===========================================================================

class TestPeriodCoverageGate:
    @staticmethod
    def _facts(*pairs):
        return {"facts": [{"field": f, "period": p, "value": 1.0} for f, p in pairs]}

    def test_current_period_present_passes(self):
        facts = self._facts(("income_statement.revenue", "2026-Q3"),
                            ("income_statement.revenue", "2025-Q3"),
                            ("segments.Intel Foundry.revenue", "2026-Q3"),
                            ("cash_flow_statement.capex", "2026-Q3-YTD"))
        ok, findings = gates.gate_period_coverage(facts, "2026-Q3")
        assert ok, findings

    def test_segments_missing_current_period_fails(self):
        facts = self._facts(("income_statement.revenue", "2026-Q3"),
                            ("segments.Intel Foundry.revenue", "2025-Q3"),
                            ("segments.Intel Foundry.revenue", "2025-Q3-YTD"))
        ok, findings = gates.gate_period_coverage(facts, "2026-Q3")
        assert not ok
        assert [f["dedup_key"] for f in findings] == ["period-missing:segments"]
        assert findings[0]["gate"] == "period_coverage"
        assert "2026-Q3" in findings[0]["summary"]

    def test_absent_statement_type_not_required_and_derived_ignored(self):
        facts = self._facts(("income_statement.revenue", "2026-Q3"),
                            ("derived.growth", "2024-Q1"))
        ok, findings = gates.gate_period_coverage(facts, "2026-Q3")
        assert ok, findings


class TestMarkerScopeMatchesWriterCard:
    IDX = {"a": "x", "b": "x"}

    def _ok(self, md):
        return gates.gate_report_evidence_links("## s\n\n" + md + "\n", self.IDX)[0]

    def test_comma_colon_dash_semicolon_period_break_a_clause(self):
        assert not self._ok("Revenue 1.5 billion, up 2.5 percent [EV:a].")
        assert not self._ok("Revenue 1.5 billion: up 2.5 percent [EV:a].")
        assert not self._ok("Revenue 1.5 billion - up 2.5 percent [EV:a].")
        assert not self._ok("Revenue 1.5 billion; up 2.5 percent [EV:a].")
        assert self._ok("Revenue 1.5 billion [EV:a], up 2.5 percent [EV:b].")

    def test_decimal_and_thousands_are_not_breaks(self):
        assert self._ok("Revenue 12,345.6 million [EV:a].")

    def test_ordinal_year_labels_are_exempt_in_any_case(self):
        for label in ("year 1", "Year 1", "Year-1", "YEAR-1", "yr 2"):
            assert self._ok(f"In {label} margin is steady.")
