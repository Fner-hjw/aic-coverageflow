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
