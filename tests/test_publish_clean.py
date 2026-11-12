"""Clean note written beside every published note."""

from __future__ import annotations

import json

import pytest

import casekit
from coverageflow import _dispatch_test_stub as _stub
from coverageflow import ledger, render_note, statestore
from tests.test_orchestrator import (_critic_findings, _writer_output,
                                     build_env, load_state)
from tests.test_owners import _events, _finding, _packet  # noqa

def orchmod_arts(n):
    from coverageflow.orchestrator import Arts
    return Arts(round=n)


# --------------------------------------------------------------- clean note
class TestCleanNoteOnPublication:
    def test_approval_writes_the_clean_note_beside_the_marked_one(self, tmp_path):
        orch, dispatch, clk, case_dir = build_env(tmp_path)
        orch.run()
        out = orch.record_approval("APPROVED", by="Supervisor")
        marked = case_dir / "report" / f"v1-{out['report_version'].split('-', 1)[1]}.md"
        clean = marked.with_name(marked.name[:-3] + ".clean.md")
        assert marked.is_file() and clean.is_file()
        assert "[EV:" in marked.read_text()
        text = clean.read_text()
        assert "[EV:" not in text and "[^1]" in text and "## Sources" in text
        assert text == render_note.render_clean(
            marked.read_text(),
            orch._evidence_index(orchmod_arts(1)))

    def test_clean_companions_are_not_counted_as_versions(self, tmp_path):
        orch, dispatch, clk, case_dir = build_env(tmp_path)
        orch.run()
        orch.record_approval("APPROVED", by="Supervisor")
        assert orch._published_versions(case_dir / "report") == 1

    def test_published_with_exceptions_also_writes_one(self, tmp_path):
        mandate = json.loads(json.dumps(casekit.MANDATE))
        mandate["limits"]["stall_rounds"] = 1
        scripts = {"critic_c1": [None, _finding(["report.md"], summary="persistent")] * 3}
        scripts["critic_c1"] = [None] + [_finding(["report.md"], summary="persistent")] * 6
        orch, dispatch, clk, case_dir = build_env(tmp_path, scripts=scripts,
                                                  mandate=mandate)
        orch.run()
        st = load_state(case_dir)
        assert st["status"] == "ESCALATED"
        ids = [f["finding_id"] for f in
               ledger.FindingsLedger(case_dir).blocking_findings()]
        out = orch.record_approval("PUBLISHED_WITH_EXCEPTIONS", by="Supervisor",
                                   reasoning="objection judged wrong",
                                   acknowledge_findings=ids)
        cleans = list((case_dir / "report").glob("v*.clean.md"))
        assert len(cleans) == 1
        text = cleans[0].read_text()
        assert "PUBLISHED WITH EXCEPTIONS" in text and "[EV:" not in text
