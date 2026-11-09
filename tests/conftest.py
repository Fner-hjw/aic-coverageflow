"""pytest conftest (owner: stream A2 tests).

Adds the repo root to sys.path so ``coverageflow`` imports without install,
and puts tests/fixtures on the path so test files can ``import casekit``.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
FIX = ROOT / "tests" / "fixtures"
if str(FIX) not in sys.path:
    sys.path.insert(0, str(FIX))
