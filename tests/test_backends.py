"""Tests for the agent layer: backends (base, cli, api, replay).

Owner: stream A3. All tests run offline - no network, no real CLI logins.
Fake CLIs live under tests/fixtures/fakeclis/ and are placed on PATH.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from coverageflow.backends import base as bbase


# --------------------------------------------------------------------------
# base.py - AgentResult / HealthReport / CostEstimate / Backend protocol
# --------------------------------------------------------------------------

class TestBaseDataclasses:
    def test_agent_result_defaults_match_contract(self):
        r = bbase.AgentResult(ok=True, backend="claude")
        assert r.output_text is None
        assert r.parsed is None
        assert r.error is None
        assert r.elapsed_seconds == 0.0
        assert r.tokens_in is None
        assert r.tokens_out is None
        assert r.raw_response_ref is None

    def test_health_report_fields(self):
        h = bbase.HealthReport(backend="replay", ok=True, detail="42 recordings")
        assert h.backend == "replay"
        assert h.ok is True
        assert h.checked_at  # ISO-8601 timestamp always recorded

    def test_cost_estimate_fields(self):
        c = bbase.CostEstimate(backend="api", estimated_tokens=1234, method="chars/4")
        assert c.estimated_tokens == 1234
        assert c.method == "chars/4"
        assert c.note is None

    def test_backend_protocol_runtime_checkable(self):
        class Stub:
            name = "stub"

            def healthcheck(self):
                return bbase.HealthReport(backend="stub", ok=True)

            def invoke(self, call, assembled_prompt):
                return bbase.AgentResult(ok=True, backend="stub")

            def estimate_cost(self, text):
                return bbase.CostEstimate(
                    backend="stub", estimated_tokens=1, method="chars/4"
                )

        assert isinstance(Stub(), bbase.Backend)

    def test_estimate_tokens_chars_per_four(self):
        assert bbase.estimate_tokens_chars_per_four("x" * 400) == 100
        # never zero for non-empty text
        assert bbase.estimate_tokens_chars_per_four("ab") >= 1
        assert bbase.estimate_tokens_chars_per_four("") == 0
