"""Which open findings a human may publish over (PUBLISHED_WITH_EXCEPTIONS)."""

import pytest

from coverageflow import ledger


@pytest.mark.parametrize("finding,expected", [
    ({"dedup_key": "critic_c1|c1.2|rounds/round-n/generator/report.md"}, False),
    ({"check_id": "C3.1"}, False),
    ({"check_id": "c2.2"}, False),
    ({"dedup_key": "critic_c2|c2.4|valuation.json"}, True),
    ({"check_id": "C4.2"}, True),
    ({"source_critic": "human", "dedup_key": "human|1|report.md"}, True),
    ({"source_critic": "gate:report_evidence_links", "dedup_key": "x"}, False),
])
def test_waivable(finding, expected):
    assert ledger.waivable(finding) is expected


def test_the_amd_exception_would_now_be_refused():
    # fnd-0007 of amd-fy2026q3r3: a cited value that is not the ledger value
    assert ledger.check_id_of(
        {"dedup_key": "critic_c1|c1.2|rounds/round-n/generator/report.md"}) == "C1.2"
    assert not ledger.waivable(
        {"dedup_key": "critic_c1|c1.2|rounds/round-n/generator/report.md"})
