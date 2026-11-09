"""Shared fixtures for agent-layer tests: fake CLI binaries on PATH."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from coverageflow.dispatch import RoleCall

FAKECLIS = Path(__file__).parent / "fixtures" / "fakeclis"


@pytest.fixture(autouse=True)
def fakecli_on_path(monkeypatch):
    """Make the fixture CLIs executable and prepend them to PATH."""
    for p in FAKECLIS.iterdir():
        p.chmod(p.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    monkeypatch.setenv("PATH", str(FAKECLIS) + os.pathsep + os.environ.get("PATH", ""))


def make_call(role: str = "ingest", timeout_seconds: float = 240.0) -> RoleCall:
    """Build a minimal, contract-shaped RoleCall for backend tests."""
    return RoleCall(
        role=role,
        request_id="req-0001",
        candidate_id=None,
        role_card={"path": f"prompts/{role}.md", "version": "1.0", "sha256": "00" * 32},
        payload={},
        output_schema=f"{role}-report.json",
        budget={"tokens_max": 60000, "timeout_seconds": timeout_seconds},
        write_scope=[f"rounds/round-001/generator/{role}-report.json"],
    )
