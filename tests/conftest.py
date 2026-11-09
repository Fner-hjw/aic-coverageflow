"""pytest conftest (merged by the coordinator).

- Adds the repo root to sys.path so ``coverageflow`` imports without install.
- Agent-layer stream (A3): fake CLI binaries on PATH for backend tests.
- make_call: minimal contract-shaped RoleCall for backend tests.
"""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
FIX = ROOT / "tests" / "fixtures"
if str(FIX) not in sys.path:
    sys.path.insert(0, str(FIX))

from coverageflow.dispatch import RoleCall  # noqa: E402

FAKECLIS = ROOT / "tests" / "fixtures" / "fakeclis"


@pytest.fixture(autouse=True)
def fakecli_on_path(monkeypatch):
    """Make the fixture CLIs executable and prepend them to PATH.

    Autouse is required by the agent-layer backend tests; controller tests
    are unaffected (they never spawn real CLIs).
    """
    if not FAKECLIS.exists():
        return
    for p in FAKECLIS.iterdir():
        p.chmod(p.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    monkeypatch.setenv("PATH", str(FAKECLIS) + os.pathsep + os.environ.get("PATH", ""))


def make_call(
    role: str = "ingest",
    timeout_seconds: float = 240.0,
    request_id: str = "req-0001",
    candidate_id: str | None = None,
) -> RoleCall:
    """Build a minimal, contract-shaped RoleCall for backend tests."""
    return RoleCall(
        role=role,
        request_id=request_id,
        candidate_id=candidate_id,
        role_card={"path": f"prompts/{role}.md", "version": "1.0", "sha256": "00" * 32},
        payload={},
        output_schema=f"{role}-report.json",
        budget={"tokens_max": 60000, "timeout_seconds": timeout_seconds},
        write_scope=[f"rounds/round-001/generator/{role}-report.json"],
    )
