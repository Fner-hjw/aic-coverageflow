"""Fan-out extraction: unit planning, compact rows, merge, orchestration."""

from __future__ import annotations

import copy
import hashlib
import json
import threading

import pytest

from coverageflow import _dispatch_test_stub as stub
from coverageflow import extraction as ex
from coverageflow import statestore
import casekit
from tests.test_orchestrator import (_critic_findings, _writer_output,
                                     build_env, load_state)

CAT = ex.load_catalogue()
SCOPE = [{"doc_id": "intc-10q-2026q3", "form": "10-Q", "filed": "2026-10-30",
          "role": "trigger"},
         {"doc_id": "intc-10k-fy2025", "form": "10-K", "filed": "2026-01-23",
          "role": "annual_anchor"}]


def _slice(doc, tag, kind):
    return {"slice_id": f"{doc}/{tag}", "kind": kind,
            "path": f"inputs/slices/{doc}/{tag}.txt", "sha256": "0" * 64,
            "anchors": {"heading": f"{kind} heading"}}


def _intel_slices():
    out = []
    for doc in ("intc-10q-2026q3", "intc-10k-fy2025"):
        out += [_slice(doc, "IS", "income_statement"),
                _slice(doc, "BS", "balance_sheet"),
                _slice(doc, "CF", "cash_flow_statement"),
                _slice(doc, "SEG", "segments"),
                _slice(doc, "MDA", "md_and_a")]
    return out


# ===================================================================== planning
class TestPlanUnits:
    def test_a_10q_plus_10k_scope_yields_four_units_each_in_order(self):
        units = ex.plan_units(SCOPE, _intel_slices())
        assert [u["unit"] for u in units] == [
            "intc-10q-2026q3/income_statement", "intc-10q-2026q3/balance_sheet",
            "intc-10q-2026q3/cash_flow", "intc-10q-2026q3/segments",
            "intc-10k-fy2025/income_statement", "intc-10k-fy2025/balance_sheet",
            "intc-10k-fy2025/cash_flow", "intc-10k-fy2025/segments"]

    def test_mda_and_full_text_slices_never_become_units(self):
        slices = _intel_slices() + [_slice("intc-10q-2026q3", "FULL", "full_text")]
        assert not [u for u in ex.plan_units(SCOPE, slices)
                    if u["kind"] in ("md_and_a", "full_text")]

    def test_a_missing_slice_kind_is_skipped(self):
        slices = [s for s in _intel_slices() if s["kind"] != "segments"]
        kinds = {u["kind"] for u in ex.plan_units(SCOPE, slices)}
        assert kinds == {"income_statement", "balance_sheet", "cash_flow"}

    def test_documents_outside_the_scope_are_ignored(self):
        slices = _intel_slices() + [_slice("intc-10q-2025q3", "IS", "income_statement")]
        assert all(u["doc_id"] != "intc-10q-2025q3"
                   for u in ex.plan_units(SCOPE, slices))

    def test_unit_carries_its_own_slice_and_filing_facts(self):
        u = ex.plan_units(SCOPE, _intel_slices())[2]
        assert u["slice"]["slice_id"] == "intc-10q-2026q3/CF"
        assert (u["form"], u["filed"], u["scope_role"]) == (
            "10-Q", "2026-10-30", "trigger")

    def test_no_slices_no_units(self):
        assert ex.plan_units(SCOPE, []) == []


# ===================================================================== rows
GOOD = "income_statement.revenue|2026-Q3|13500|USDmm|line 3"


class TestRowParsing:
    def test_a_good_row_parses(self):
        got, err = ex.parse_row(GOOD, CAT)
        assert err is None
        assert got == {"field": "income_statement.revenue", "period": "2026-Q3",
                       "value": 13500.0, "unit": "USDmm", "locator": "line 3"}

    def test_the_locator_may_contain_a_pipe(self):
        got, err = ex.parse_row(GOOD.replace("line 3", "Revenue | Net"), CAT)
        assert err is None and got["locator"] == "Revenue | Net"

    @pytest.mark.parametrize("row,needle", [
        ("income_statement.revenue|2026-Q3|13500|USDmm", "expected 5"),
        ("revenue|2026-Q3|13500|USDmm|l1", "unknown field 'revenue'"),
        ("income_statement.revenue|FY2026|13500|USDmm|l1", "not canonical"),
        ("income_statement.revenue|2026Q3|13500|USDmm|l1", "not canonical"),
        ("income_statement.revenue|2026-Q3|13,500|USDmm|l1", "not a plain number"),
        ("income_statement.revenue|2026-Q3|(650)|USDmm|l1", "not a plain number"),
        ("income_statement.revenue|2026-Q3|n/a|USDmm|l1", "not a plain number"),
        ("income_statement.revenue|2026-Q3|13500|dollars|l1", "unit 'dollars'"),
        ("income_statement.revenue|2026-Q3|13500|USDmm| ", "locator is empty"),
    ])
    def test_rejections_say_what_is_wrong(self, row, needle):
        got, err = ex.parse_row(row, CAT)
        assert got is None and needle in err

    def test_a_non_string_row_is_rejected(self):
        assert "not a string" in ex.parse_row({"field": "x"}, CAT)[1]

    def test_segment_fields_use_the_as_reported_name(self):
        for f in ("segments.Data Center and AI.revenue",
                  "segments.ccg.operating_income",
                  "segments.all_other_revenue"):
            assert ex.field_known(f, CAT), f
        assert not ex.field_known("segments.Data Center and AI.margin", CAT)


def _unit(kind="income_statement", doc="intc-10q-2026q3", filed="2026-10-30"):
    return {"unit": f"{doc}/{kind}", "doc_id": doc, "kind": kind,
            "slice_id": f"{doc}/IS", "form": "10-Q", "filed": filed,
            "slice": {"anchors": {"heading": "Consolidated Statements of Operations"}}}


class TestExpandUnit:
    def test_rows_expand_to_fully_cited_facts(self):
        err, got = ex.expand_unit({"rows": [GOOD], "missing": [], "notes": []},
                                  _unit(), CAT)
        assert err is None
        assert got["facts"] == [{
            "field": "income_statement.revenue", "period": "2026-Q3",
            "value": 13500.0, "unit": "USDmm", "confidence": 1.0,
            "citation": {"doc_id": "intc-10q-2026q3",
                         "slice_id": "intc-10q-2026q3/IS",
                         "anchor": "Consolidated Statements of Operations",
                         "locator": "line 3"}}]

    def test_every_bad_row_is_named_in_one_message(self):
        err, got = ex.expand_unit({"rows": [
            GOOD, "bogus|2026-Q3|1|USDmm|l", "income_statement.revenue|Q3|1|USDmm|l"],
            "missing": [], "notes": []}, _unit(), CAT)
        assert got is None
        assert "2 invalid entries" in err and "rows[1]" in err and "rows[2]" in err
        assert "intc-10q-2026q3/income_statement" in err

    def test_a_duplicate_field_period_is_rejected(self):
        err, _ = ex.expand_unit({"rows": [GOOD, GOOD]}, _unit(), CAT)
        assert "duplicates rows[0]" in err

    def test_no_rows_and_nothing_missing_is_rejected(self):
        err, _ = ex.expand_unit({"rows": []}, _unit(), CAT)
        assert "no rows and no missing" in err

    def test_missing_only_is_accepted(self):
        err, got = ex.expand_unit({"rows": [], "missing": [
            {"field": "income_statement.interest_expense", "reason": "not printed"}]},
            _unit(), CAT)
        assert err is None and got["facts"] == []

    def test_rows_must_be_an_array(self):
        err, _ = ex.expand_unit({"rows": "a|b"}, _unit(), CAT)
        assert "rows must be an array" in err

    def test_the_error_list_is_capped(self):
        rows = [f"bogus{i}|2026-Q3|1|USDmm|l" for i in range(40)]
        err, _ = ex.expand_unit({"rows": rows}, _unit(), CAT)
        assert "40 invalid entries (+28 more)" in err


# ===================================================================== merge
def _done(unit, facts, missing=(), notes=(), req="req-1"):
    return {"unit": unit, "request_id": req, "facts": list(facts),
            "missing": list(missing), "notes": list(notes)}


def _f(field, period, value, doc="intc-10q-2026q3"):
    return {"field": field, "period": period, "value": value, "unit": "USDmm",
            "confidence": 1.0, "citation": {"doc_id": doc, "slice_id": f"{doc}/IS",
                                            "anchor": "a", "locator": "l"}}


Q = _unit(doc="intc-10q-2026q3", filed="2026-10-30")
K = _unit(doc="intc-10k-fy2025", filed="2026-01-23")


def _merge(done):
    return ex.merge_units(done, request_id="req-1", candidate_id=None,
                          prompt_version="1.6")


class TestMerge:
    def test_facts_concatenate_in_unit_order_with_the_artifact_header(self):
        out = _merge([_done(Q, [_f("income_statement.revenue", "2026-Q3", 1)]),
                      _done(K, [_f("income_statement.revenue", "2025", 2, "intc-10k-fy2025")],
                            req="req-2")])
        assert [f["period"] for f in out["facts"]] == ["2026-Q3", "2025"]
        assert (out["role"], out["candidate_id"], out["conformance"]) == (
            "extractor", None, "schema 1.0")
        assert out["prompt_version"] == "1.6" and out["notes"] == []

    def test_provenance_names_every_unit_and_request(self):
        out = _merge([_done(Q, [_f("a.b", "2026-Q3", 1)], req="req-7"),
                      _done(K, [_f("a.c", "2025", 2)], req="req-9")])
        assert [(u["unit"], u["request_id"]) for u in out["extraction_units"]] == [
            ("intc-10q-2026q3/income_statement", "req-7"),
            ("intc-10k-fy2025/income_statement", "req-9")]

    def test_equal_duplicates_across_documents_keep_one(self):
        out = _merge([_done(Q, [_f("income_statement.revenue", "2025", 100.0)]),
                      _done(K, [_f("income_statement.revenue", "2025", 100.0,
                                   "intc-10k-fy2025")])])
        assert len(out["facts"]) == 1 and out["notes"] == []

    def test_a_restated_figure_takes_the_newer_filing_and_says_so(self):
        out = _merge([_done(K, [_f("income_statement.revenue", "2025", 100.0,
                                   "intc-10k-fy2025")]),
                      _done(Q, [_f("income_statement.revenue", "2025", 105.0)])])
        (fact,) = out["facts"]
        assert fact["value"] == 105.0
        assert fact["citation"]["doc_id"] == "intc-10q-2026q3"
        (note,) = out["notes"]
        assert "restated" in note and "intc-10q-2026q3" in note \
            and "intc-10k-fy2025" in note and "105" in note and "100" in note

    def test_the_newer_filing_wins_whichever_unit_comes_first(self):
        out = _merge([_done(Q, [_f("income_statement.revenue", "2025", 105.0)]),
                      _done(K, [_f("income_statement.revenue", "2025", 100.0,
                                   "intc-10k-fy2025")])])
        assert out["facts"][0]["value"] == 105.0 and len(out["notes"]) == 1

    def test_a_conflict_inside_one_document_keeps_the_first_and_is_noted(self):
        a, b = _unit("income_statement"), _unit("segments")
        out = _merge([_done(a, [_f("income_statement.revenue", "2026-Q3", 1.0)]),
                      _done(b, [_f("income_statement.revenue", "2026-Q3", 2.0)])])
        assert out["facts"][0]["value"] == 1.0
        assert "conflict within intc-10q-2026q3" in out["notes"][0]

    def test_missing_is_dropped_when_another_unit_supplied_the_field(self):
        miss = {"field": "income_statement.revenue", "reason": "not in this slice"}
        keep = {"field": "income_statement.interest_expense", "reason": "not printed"}
        out = _merge([_done(Q, [], missing=[miss, keep]),
                      _done(K, [_f("income_statement.revenue", "2025", 1.0)])])
        assert out["missing"] == [keep]

    def test_a_period_scoped_missing_needs_that_exact_period(self):
        m = {"field": "income_statement.revenue", "reason": "x", "period": "2024"}
        out = _merge([_done(Q, [_f("income_statement.revenue", "2025", 1.0)],
                            missing=[m])])
        assert out["missing"] == [m]
        out = _merge([_done(Q, [_f("income_statement.revenue", "2024", 1.0)],
                            missing=[m])])
        assert out["missing"] == []

    def test_identical_missing_entries_collapse_and_unit_notes_are_kept(self):
        m = {"field": "income_statement.interest_expense", "reason": "not printed"}
        out = _merge([_done(Q, [], missing=[m], notes=["n1"]),
                      _done(K, [], missing=[dict(m)], notes=["n1", "n2"])])
        assert out["missing"] == [m] and out["notes"] == ["n1", "n2"]


# =============================================================== orchestration
Q_DOC = "intc-10q-2026q3"
K_DOC = "intc-10k-fy2025"


def two_doc_manifest():
    m = copy.deepcopy(casekit.MANIFEST)
    m["documents"].append({
        "doc_id": K_DOC, "form": "10-K", "filed": "2026-01-23",
        "period": "2025-12-27", "path": f"inputs/{K_DOC}.htm",
        "sha256": "e" * 64, "bytes": 100, "source_url": "https://example.com/k"})
    return m


def eight_slice_ingest(payload):
    """An ingest report that declares IS/BS/CF/SEG/MDA for both filings."""
    slices = []
    for doc in (Q_DOC, K_DOC):
        text = stub._slice_text(doc)
        for tag, kind in (("IS", "income_statement"), ("BS", "balance_sheet"),
                          ("CF", "cash_flow_statement"), ("SEG", "segments"),
                          ("MDA", "md_and_a")):
            slices.append({"slice_id": f"{doc}/{tag}", "kind": kind,
                           "path": f"inputs/slices/{doc}/{tag}.txt",
                           "sha256": hashlib.sha256(text.encode()).hexdigest(),
                           "anchors": {"heading": f"{kind} heading"}})
    return {"role": "ingest", "doc_id": Q_DOC, "candidate_id": None,
            "classification": {"form": "10-Q", "affects": ["extraction"],
                               "reason": "new quarter"},
            "slices": slices, "superseded": [],
            "conformance": "schema 1.0", "prompt_version": "1.0"}


def env(tmp_path, scripts=None):
    scripts = dict(scripts or {})
    scripts.setdefault("ingest", eight_slice_ingest)
    return build_env(tmp_path, scripts=scripts, manifest=two_doc_manifest())


def events(case_dir, name):
    return [e for e in statestore.read_trace(case_dir) if e.get("event") == name]


class TestFanOut:
    def test_the_extractor_is_dispatched_once_per_unit(self, tmp_path):
        orch, dispatch, clk, case_dir = env(tmp_path)
        orch.run()
        units = [c.payload["unit"]["name"] for c in dispatch.calls_of("extractor")]
        assert sorted(units) == sorted(
            f"{d}/{k}" for d in (Q_DOC, K_DOC)
            for k in ("income_statement", "balance_sheet", "cash_flow", "segments"))

    def test_all_units_go_through_one_concurrent_dispatch(self, tmp_path):
        orch, dispatch, clk, case_dir = env(tmp_path)
        seen = []
        real = orch._dispatch_concurrently

        def spy(calls):
            seen.append([c.role for c in calls])
            return real(calls)

        orch._dispatch_concurrently = spy
        orch.run()
        batches = [b for b in seen if b and b[0] == "extractor"]
        assert batches == [["extractor"] * 8]

    def test_units_really_run_in_parallel(self, tmp_path):
        orch, dispatch, clk, case_dir = env(tmp_path)
        barrier = threading.Barrier(8, timeout=10)
        real = dispatch.__call__

        def gated(call):
            if call.role == "extractor":
                barrier.wait()        # only passes if all 8 are in flight
            return real(call)

        orch.dispatch = gated
        orch.dispatch.calls_of = dispatch.calls_of
        orch.run()
        assert len(dispatch.calls_of("extractor")) == 8

    def test_each_unit_sees_only_its_own_slice_and_fields(self, tmp_path):
        orch, dispatch, clk, case_dir = env(tmp_path)
        orch.run()
        for call in dispatch.calls_of("extractor"):
            slices = call.payload["prior_artifacts"]["slices"]
            assert [s["slice_id"] for s in slices] == [call.payload["unit"]["slice_id"]]
            assert slices[0]["text"]
            kind = call.payload["unit"]["kind"]
            fields = call.payload["fields"]
            if kind == "segments":
                assert all(f.startswith("segments.") for f in fields)
            else:
                assert fields and all(f.startswith(kind + ".") for f in fields)
            assert "mandate" not in call.payload
            assert "ingest_report" not in call.payload["prior_artifacts"]

    def test_the_merged_facts_json_has_the_pipeline_shape_and_provenance(self, tmp_path):
        orch, dispatch, clk, case_dir = env(tmp_path)
        orch.run()
        facts = statestore.read_json(
            case_dir / "rounds" / "round-001" / "generator" / "facts.json")
        assert facts["role"] == "extractor"
        # eight stub units all answer with the same figures: they collapse
        keys = [(f["field"], f["period"]) for f in facts["facts"]]
        assert len(keys) == len(set(keys))
        assert all(set(f) >= {"field", "period", "value", "unit", "confidence",
                              "citation"} for f in facts["facts"])
        ids = {u["request_id"] for u in facts["extraction_units"]}
        assert len(facts["extraction_units"]) == 8
        assert ids == {c.request_id for c in dispatch.calls_of("extractor")}
        assert facts["request_id"] in ids
        assert facts["provenance"]["cutoff_date"]
        assert load_state(case_dir)["status"] == "PENDING_APPROVAL"

    def test_trace_has_one_dispatched_and_one_result_per_unit(self, tmp_path):
        orch, dispatch, clk, case_dir = env(tmp_path)
        orch.run()
        disp = [e for e in events(case_dir, "AGENT_DISPATCHED")
                if e["role"] == "extractor"]
        res = [e for e in events(case_dir, "AGENT_RESULT")
               if e["role"] == "extractor"]
        assert len(disp) == len(res) == 8
        assert {e["detail"]["unit"] for e in disp} == {e["detail"]["unit"] for e in res}
        assert len({e["detail"]["unit"] for e in res}) == 8

    def test_result_detail_records_model_and_effort(self, tmp_path):
        orch, dispatch, clk, case_dir = env(tmp_path)
        orch.run()
        by_role = {}
        for e in events(case_dir, "AGENT_RESULT"):
            by_role.setdefault(e["role"], e)
        assert by_role["extractor"]["detail"]["reasoning_effort"] == "medium"
        assert by_role["writer"]["detail"]["reasoning_effort"] is None
        assert "model" in by_role["writer"]["detail"]

    def test_tokens_accumulate_under_the_extractor_role(self, tmp_path):
        orch, dispatch, clk, case_dir = env(tmp_path)
        orch.run()
        used = load_state(case_dir)["budget"]["used"]["tokens_by_role"]
        assert used["extractor"] == 8 * (140 + 260)

    def test_the_extractor_call_requests_medium_effort(self, tmp_path):
        orch, dispatch, clk, case_dir = env(tmp_path)
        orch.run()
        assert all(c.budget["reasoning_effort"] == "medium"
                   for c in dispatch.calls_of("extractor"))
        assert "reasoning_effort" not in dispatch.calls_of("writer")[0].budget

    def test_a_mandate_can_change_or_clear_the_role_effort(self, tmp_path):
        mandate = copy.deepcopy(casekit.MANDATE)
        mandate["limits"]["role_efforts"] = {"extractor": "low", "writer": "high"}
        orch, dispatch, clk, case_dir = build_env(tmp_path, mandate=mandate)
        orch.run()
        assert dispatch.calls_of("extractor")[0].budget["reasoning_effort"] == "low"
        assert dispatch.calls_of("writer")[0].budget["reasoning_effort"] == "high"
        assert "reasoning_effort" not in dispatch.calls_of("model")[0].budget

    def test_no_declared_statement_slice_blocks_instead_of_dispatching(self, tmp_path):
        def only_mda(payload):
            rep = eight_slice_ingest(payload)
            rep["slices"] = [s for s in rep["slices"] if s["kind"] == "md_and_a"]
            return rep

        orch, dispatch, clk, case_dir = env(tmp_path, scripts={"ingest": only_mda})
        orch.run()
        st = load_state(case_dir)
        assert st["status"] == "BLOCKED" and "nothing to read" in st["reason"]["message"]
        assert not dispatch.calls_of("extractor")


class TestPerUnitRepair:
    def _flaky(self, unit_suffix, bad_rows, times=1):
        """Answer the named unit with bad rows `times` times, then normally."""
        left = {"n": times}
        lock = threading.Lock()

        def script(payload):
            if payload["unit"]["name"].endswith(unit_suffix):
                with lock:
                    if left["n"] > 0:
                        left["n"] -= 1
                        return {"role": "extractor",
                                "unit": payload["unit"]["name"], "rows": bad_rows,
                                "missing": [], "notes": [],
                                "conformance": "schema 1.0", "prompt_version": "1.6"}
            return stub._default_facts(payload)

        return script

    def test_only_the_invalid_unit_is_re_requested_with_feedback(self, tmp_path):
        script = self._flaky(f"{K_DOC}/cash_flow",
                             ["cash_flow.net_income|FY2025|10|USDmm|l1",
                              "cash_flow.net_income|2025|1,000|USDmm|l1"])
        orch, dispatch, clk, case_dir = env(tmp_path, scripts={"extractor": script})
        orch.run()
        st = load_state(case_dir)
        assert st["status"] == "PENDING_APPROVAL"
        calls = dispatch.calls_of("extractor")
        assert len(calls) == 9                       # 8 units + one repair
        repair = [c for c in calls if c.payload.get("submission_feedback")]
        assert len(repair) == 1
        assert repair[0].payload["unit"]["name"] == f"{K_DOC}/cash_flow"
        fb = repair[0].payload["submission_feedback"]
        assert "rows[0]" in fb and "rows[1]" in fb and "not canonical" in fb and "not a plain number" in fb
        assert len({c.request_id for c in calls}) == 9   # a new request each time
        # repaired without consuming a round
        assert st["budget"]["used"]["rounds"] == 1
        assert st["budget"]["used"]["retries_by_role"][f"extractor:{K_DOC}/cash_flow"] == 1

    def test_a_unit_that_never_validates_exhausts_its_retries(self, tmp_path):
        script = self._flaky(f"{Q_DOC}/segments", ["nonsense"], times=99)
        orch, dispatch, clk, case_dir = env(tmp_path, scripts={"extractor": script})
        orch.run()
        st = load_state(case_dir)
        assert st["status"] == "EXHAUSTED" and st["reason"]["code"] == "retries"
        assert "extractor" in st["reason"]["message"]

    def test_a_stale_request_echo_repairs_only_that_unit(self, tmp_path):
        seen = {"n": 0}
        lock = threading.Lock()

        def script(payload):
            out = stub._default_facts(payload)
            with lock:
                seen["n"] += 1
                if seen["n"] == 3:
                    out["request_id"] = "req-stale-9999"
            return out

        orch, dispatch, clk, case_dir = env(tmp_path, scripts={"extractor": script})
        orch.run()
        assert len(dispatch.calls_of("extractor")) == 9
        assert load_state(case_dir)["status"] == "PENDING_APPROVAL"


class TestReuseAcrossRounds:
    def test_a_rebuild_reuses_the_merged_facts_by_hash_proof(self, tmp_path):
        finding = _critic_findings()
        finding["findings"][0]["affected_artifacts"] = ["report.md"]
        alt = {"role": "writer", "report_md": stub.ALT_REPORT_MD,
               "conformance": "schema 1.0", "prompt_version": "1.0"}
        scripts = {"critic_c1": [None, finding],
                   "writer": [_writer_output(stub.DEFAULT_REPORT_MD), alt]}
        orch, dispatch, clk, case_dir = env(tmp_path, scripts=scripts)
        orch.run()
        st = load_state(case_dir)
        assert st["budget"]["used"]["rounds"] == 2
        assert len(dispatch.calls_of("extractor")) == 8      # round 1 only
        assert events(case_dir, "EXTRACTOR_REUSED")
        r1 = statestore.read_json(
            case_dir / "rounds" / "round-001" / "generator" / "facts.json")
        r2 = statestore.read_json(
            case_dir / "rounds" / "round-003" / "generator" / "facts.json")
        assert r1["facts"] == r2["facts"]
        assert r2["extraction_units"] == r1["extraction_units"]

    def test_a_finding_against_facts_re_runs_every_unit(self, tmp_path):
        alt = {"role": "writer", "report_md": stub.ALT_REPORT_MD,
               "conformance": "schema 1.0", "prompt_version": "1.0"}
        scripts = {"critic_c1": [None, _critic_findings()],   # names facts.json
                   "writer": [_writer_output(stub.DEFAULT_REPORT_MD), alt]}
        orch, dispatch, clk, case_dir = env(tmp_path, scripts=scripts)
        orch.run()
        assert len(dispatch.calls_of("extractor")) == 16
