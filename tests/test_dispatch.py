"""Tests for dispatch.py: RoleCall envelope + strict parse + budget enforcement.

Owner: stream A3. Uses a StubBackend (real Backend protocol shape) - never a
real CLI or network.
"""

from __future__ import annotations

import pytest


# --------------------------------------------------------------------------
# RoleCall envelope (contracts interfaces s1) - single source of truth
# --------------------------------------------------------------------------

class TestRoleCall:
    def test_fields_match_frozen_contract(self):
        from coverageflow.dispatch import RoleCall

        call = RoleCall(
            role="extractor",
            request_id="req-0007",
            candidate_id=None,
            role_card={
                "path": "prompts/extractor.md",
                "version": "1.0",
                "sha256": "ab" * 32,
            },
            payload={"slices": []},
            output_schema="facts.json",
            budget={"tokens_max": 60000, "timeout_seconds": 240},
            write_scope=["rounds/round-001/generator/facts.json"],
        )
        assert call.role == "extractor"
        assert call.request_id == "req-0007"
        assert call.role_card["version"] == "1.0"
        assert call.budget["timeout_seconds"] == 240
        assert call.write_scope == ["rounds/round-001/generator/facts.json"]

    def test_frozen(self):
        from coverageflow.dispatch import RoleCall

        call = RoleCall(
            role="ingest",
            request_id="req-0001",
            candidate_id="cand-1",
            role_card={"path": "prompts/ingest.md", "version": "1.0", "sha256": "00"},
            payload={},
            output_schema="ingest-report.json",
            budget={"tokens_max": 60000, "timeout_seconds": 60},
            write_scope=["rounds/round-001/generator/ingest-report.json"],
        )
        with pytest.raises(Exception):
            call.role = "model"  # frozen: orchestrator binds once


# --------------------------------------------------------------------------
# dispatch_role(): assembly + invocation + strict parse + budgets + trace
# --------------------------------------------------------------------------

import json
from pathlib import Path

from coverageflow import _prompt_assembly as pa
from coverageflow import dispatch as disp


VALID_INGEST = json.dumps(
    {
        "role": "ingest",
        "request_id": "req-0001",
        "candidate_id": None,
        "doc_id": "toy-10q-2026q3",
        "classification": {
            "form": "10-Q",
            "affects": ["extraction", "model"],
            "reason": "new quarterly statements",
        },
        "slices": [
            {
                "slice_id": "toy-10q-2026q3/IS",
                "kind": "income_statement",
                "path": "inputs/slices/toy-10q-2026q3/IS.txt",
                "sha256": "ab",
                "anchors": {"first_line": "Consolidated Statements of Operations"},
            }
        ],
        "superseded": [],
        "conformance": "schema 1.0",
        "prompt_version": "1.0",
    }
)


class StubBackend:
    """Backend double for dispatch tests: returns canned text, records calls."""

    name = "stub"

    def __init__(self, output_text=VALID_INGEST, ok=True,
                 tokens_out=None, raise_miss=False):
        self.output_text = output_text
        self.ok = ok
        self.tokens_out = tokens_out
        self.raise_miss = raise_miss
        self.invoked_prompts = []

    def invoke(self, call, assembled_prompt):
        from coverageflow.backends import replay_backends as rp

        if self.raise_miss:
            raise rp.ReplayMissError(call.role, "x" * 64, 0)
        self.invoked_prompts.append(assembled_prompt)
        tokens_out = self.tokens_out
        if tokens_out is None:
            tokens_out = (len(self.output_text) // 4) if self.output_text else 0
        return disp.AgentResult(
            ok=self.ok,
            output_text=self.output_text,
            backend=self.name,
            elapsed_seconds=0.01,
            tokens_in=len(assembled_prompt) // 4,
            tokens_out=tokens_out,
        )

    def healthcheck(self):
        return disp.HealthReport(backend=self.name, ok=True)

    def estimate_cost(self, text):
        return None


@pytest.fixture
def tree(tmp_path):
    (tmp_path / "prompts").mkdir()
    (tmp_path / "references").mkdir()
    pa.write_text(tmp_path / "references" / "harness-preamble.md", "PREAMBLE\n")
    pa.write_text(
        tmp_path / "prompts" / "ingest.md",
        "--version: 1.0\n"
        "--role: ingest\n"
        "--produces: ingest-report.json\n"
        "--read: mandate.json\n"
        "--references:\n"
        "\n# Data & Ingest\n",
    )
    return tmp_path


def build_call(**over):
    params = dict(
        role="ingest",
        request_id="req-0001",
        candidate_id="cand-1",
        role_card={"path": "prompts/ingest.md", "version": "1.0", "sha256": None},
        payload={"mandate": {"ticker": "TOY"}, "doc_id": "toy-10q-2026q3"},
        output_schema="ingest-report.json",
        budget={"tokens_max": 60000, "timeout_seconds": 240},
        write_scope=["rounds/round-001/generator/ingest-report.json"],
    )
    params.update(over)
    return disp.RoleCall(**params)


class TestDispatchHappyPath:
    def test_valid_output_parsed_and_traced(self, tree):
        backend = StubBackend()
        result, trace = disp.dispatch_role(build_call(), backend, root=str(tree))
        assert result.ok, result.error
        assert result.parsed is not None
        assert result.parsed["role"] == "ingest"
        assert result.backend == "stub"
        assert result.error is None

        assert trace["event"] == "AGENT_RESULT"
        assert trace["request_id"] == "req-0001"
        assert trace["candidate_id"] == "cand-1"
        assert trace["role"] == "ingest"
        assert trace["backend"] == "stub"
        assert trace["tokens_in"] == result.tokens_in
        assert trace["tokens_out"] == result.tokens_out
        assert trace["detail"]["ok"] is True
        assert trace["detail"]["assembled_prompt_sha256"]
        assert trace["prompt_versions"] == {"role_card": "1.0"}

    def test_assembly_deterministic_through_dispatch(self, tree):
        backend = StubBackend()
        r1, t1 = disp.dispatch_role(build_call(), backend, root=str(tree))
        r2, t2 = disp.dispatch_role(build_call(), backend, root=str(tree))
        assert (
            t1["detail"]["assembled_prompt_sha256"]
            == t2["detail"]["assembled_prompt_sha256"]
        )
        assert backend.invoked_prompts[0] == backend.invoked_prompts[1]

    def test_replay_miss_propagates(self, tree):
        backend = StubBackend(raise_miss=True)
        with pytest.raises(Exception) as excinfo:
            disp.dispatch_role(build_call(), backend, root=str(tree))
        assert "ReplayMiss" in str(excinfo.value)


class TestStrictParsing:
    def test_markdown_fenced_json_tolerated(self, tree):
        fenced = "```json\n" + VALID_INGEST + "\n```"
        backend = StubBackend(output_text=fenced)
        result, _ = disp.dispatch_role(build_call(), backend, root=str(tree))
        assert result.ok, result.error
        assert result.parsed["role"] == "ingest"

    def test_prose_wrapped_json_fenced_only(self, tree):
        wrapped = "Here is my output:\n```json\n" + VALID_INGEST + "\n```\nThanks."
        backend = StubBackend(output_text=wrapped)
        result, _ = disp.dispatch_role(build_call(), backend, root=str(tree))
        assert result.ok, result.error

    def test_non_json_output_repairable_error(self, tree):
        backend = StubBackend(output_text="I could not complete the task in JSON.")
        result, _ = disp.dispatch_role(build_call(), backend, root=str(tree))
        assert result.ok is False
        assert result.parsed is None
        assert result.error and "repair" in result.error.lower()
        assert result.output_text == "I could not complete the task in JSON."

    def test_schema_invalid_output_repairable_error(self, tree):
        doc = json.loads(VALID_INGEST)
        del doc["slices"]
        del doc["classification"]
        backend = StubBackend(output_text=json.dumps(doc))
        result, _ = disp.dispatch_role(build_call(), backend, root=str(tree))
        assert result.ok is False
        assert "slices" in result.error and "classification" in result.error
        assert "repair" in result.error.lower()
        assert result.parsed is None

    def test_non_dict_json_repairable_error(self, tree):
        backend = StubBackend(output_text="[1, 2, 3]")
        result, _ = disp.dispatch_role(build_call(), backend, root=str(tree))
        assert result.ok is False
        assert "repair" in result.error.lower()

    def test_unknown_schema_ref_fails_closed_without_invoking(self, tree):
        """An unregistered ref is a wiring bug: block, do not accept anything."""
        backend = StubBackend(output_text=json.dumps({"anything": "goes", "n": 1}))
        result, trace = disp.dispatch_role(
            build_call(output_schema="unknown-artifact.json"), backend, root=str(tree)
        )
        assert result.ok is False
        assert result.blocking is True
        assert "unknown output schema ref" in result.error
        assert backend.invoked_prompts == [], "no time may be spent on the model"
        assert trace["detail"]["ok"] is False

    def test_parse_output_strict_rejects_unknown_ref(self):
        parsed, err = disp.parse_output_strict('{"a": 1}', "unknown-artifact.json")
        assert parsed is None
        assert err and "unknown output schema ref" in err

    def test_role_output_schema_covers_every_role(self):
        for role in ("ingest", "extractor", "model", "writer", "critic_c1", "critic_c4"):
            assert disp.output_schema_for(role) in disp._SCHEMA_REQUIRED_KEYS
        with pytest.raises(LookupError):
            disp.output_schema_for("nobody")

    def test_writer_report_envelope_schema(self, tree):
        writer_card = tree / "prompts" / "writer.md"
        pa.write_text(
            writer_card,
            "--version: 1.0\n--role: writer\n--produces: report.md\n"
            "--read: facts, model, valuation\n--references:\n\n# Report Draft\n",
        )
        payload = json.dumps(
            {
                "role": "writer",
                "request_id": "req-0002",
                "candidate_id": "cand-1",
                "report_md": "# Quarterly update\nClaim cited [cite:ev-1].",
                "citations": ["ev-1"],
                "conformance": "schema 1.0",
                "prompt_version": "1.0",
            }
        )
        backend = StubBackend(output_text=payload, ok=True)
        result, _ = disp.dispatch_role(
            build_call(
                role="writer",
                role_card={"path": "prompts/writer.md", "version": "1.0", "sha256": None},
                output_schema="report.md",
                write_scope=["report/v1-cand1.md"],
            ),
            backend,
            root=str(tree),
        )
        assert result.ok, result.error
        assert result.parsed["report_md"].startswith("# Quarterly update")


class TestBudgetEnforcement:
    def test_token_cap_exceeded_repairable_error(self, tree):
        backend = StubBackend(tokens_out=99_999)
        result, _ = disp.dispatch_role(
            build_call(budget={"tokens_max": 100, "timeout_seconds": 240}),
            backend,
            root=str(tree),
        )
        assert result.ok is False
        assert "token" in result.error.lower()
        # output preserved so the orchestrator can record what blew the cap
        assert result.output_text == VALID_INGEST

    def test_backend_failure_passes_through(self, tree):
        backend = StubBackend(output_text=None, ok=False)
        backend.invoke.__self__  # exists
        result, trace = disp.dispatch_role(build_call(), backend, root=str(tree))
        assert result.ok is False
        assert trace["detail"]["ok"] is False


def test_mandate_approval_edits_are_not_file_references(tmp_path):
    from coverageflow.dispatch import missing_payload_inputs
    payload = {"mandate": {"approval": {"by": "Analyst", "edits": [
        {"path": "assumptions.exit_multiple_ebitda", "proposed": 20.1, "final": 15.5}]},
        "provenance": {"assumptions.wacc": {"path": "assumptions.wacc"}}},
        "inputs": [{"path": "inputs/missing.htm"}]}
    assert missing_payload_inputs(payload, tmp_path) == ["inputs/missing.htm (absent)"]
