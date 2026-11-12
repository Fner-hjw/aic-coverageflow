"""Smoke tests for export_dossier (owner: stream A5).

Renders the research note from a minimal, contract-shaped case tree (same
artifact shapes the orchestrator produces), asserts every template token is
filled and the key numbers land verbatim from the evidence artifacts, and
when pdflatex is installed compiles the PDF.  Layout and content of the note
itself are covered by tests/test_report_doc.py.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from coverageflow.export_dossier import build_dossier, collect_case, render_dossier_tex

CUT = "intc-fy2026q3-20261030"


def _write(path: Path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=1) + "\n", encoding="utf-8")


def make_mini_case(root: Path) -> Path:
    case = root / "cases" / CUT
    _write(case / "mandate.json", {
        "mandate_id": "man-x", "case_id": CUT, "ticker": "INTC",
        "cutoff_date": "2026-10-30",
        "report_policy": {"sections": ["Summary", "Results",
                                       "Segment detail", "Outlook",
                                       "Valuation", "Risks"]},
        "assumptions": {"as_of": "2026-10-30", "wacc": 0.104,
                        "terminal_growth": 0.025,
                        "exit_multiple_ebitda": 14.0,
                        "peer_block": {"as_of": "2026-10-30", "peers": []}}})
    _write(case / "state.json", {
        "case_id": CUT, "status": "CLOSED", "pending": {},
        "candidate": {"candidate_id": "cand-0123456789abcd", "built_round": 1,
                      "clean_review_rounds": 2}})
    _write(case / "result.json", {
        "case_id": CUT, "terminal": "APPROVED",
        "candidate_id": "cand-0123456789abcd",
        "report_version": "v1-cand-012",
        "reason": {"code": "APPROVED", "message": "human approved"},
        "closed_ts": "2026-11-09T17:00:00Z"})
    _write(case / "inputs" / "manifest.json", {
        "as_of_cutoff": "2026-10-30",
        "documents": [{"doc_id": "intc-10q-2026q3", "form": "10-Q",
                       "filed": "2026-10-30",
                       "sha256": "a" * 64, "bytes": 1834512}],
        "bundle_id": "intc-2026-10"})
    gen = case / "rounds" / "round-001" / "generator"
    _write(gen / "model.json", {
        "artifact": "model", "engine": "model_engine@1.0",
        "candidate_id": "cand-0123456789abcd", "cutoff": "2026-10-30",
        "series": {
            "income_statement.revenue": {"2026-Q3": 13500.0, "2025-Q3": 12500.0},
            "income_statement.gross_profit": {"2026-Q3": 5175.0, "2025-Q3": 4375.0},
            "income_statement.operating_income": {"2026-Q3": 825.0, "2025-Q3": 175.0},
            "income_statement.net_income": {"2026-Q3": 700.0, "2025-Q3": 150.0},
            "income_statement.diluted_eps": {"2026-Q3": 0.16, "2025-Q3": 0.03},
            "balance_sheet.cash_and_equivalents": {"2026-Q3": 25000.0, "2025-Q3": 21000.0},
            "balance_sheet.long_term_debt": {"2026-Q3": 50000.0, "2025-Q3": 48000.0},
            "segments.ccg.revenue": {"2026-Q3": 24000.0, "FY2025": 30000.0},
            "segments.ccg.operating_income": {"2026-Q3": 1500.0},
        }})
    _write(gen / "valuation.json", {
        "artifact": "valuation", "engine": "valuation_engine@2.0",
        "candidate_id": "cand-0123456789abcd", "cutoff": "2026-10-30",
        "assumptions": {"wacc": 0.104, "terminal_growth": 0.025},
        "dcf": {"base": {"period": "FY2025", "revenue": 52000.0, "fcf": 1200.0},
                "years": [{"year": 1, "revenue": 54000.0, "fcf": 1400.0,
                           "discount_factor": 0.906, "pv": 1268.0}],
                "pv_sum": 1268.0,
                "ev": {"exit_multiple": 90000.0, "gordon": 45000.0},
                "equity_value": {"exit_multiple": 63000.0, "gordon": 18000.0},
                "per_diluted_share": {"exit_multiple": 14.40, "gordon": 4.11},
                "net_debt": 27000.0, "diluted_shares": 4375.0,
                "discount_convention": "year_end", "wacc": 0.104},
        "comps": {"as_of": "2026-10-30", "peers": [], "applied": {},
                  "comps_price": None},
        "sensitivity": {"basis": "gordon",
                        "wacc_points": [0.064, 0.094],
                        "terminal_growth_points": [0.005, 0.025],
                        "grid": [[12.60, 16.60], [7.60, 11.10]]},
        "cross_check_note": "no peer block; DCF tolerance check active.",
        "within_tolerance": True})
    _write(gen / "candidate-manifest.json", {
        "request_id": "req-0006", "candidate_id": "cand-0123456789abcd",
        "round": 1,
        "reports": [{"role": "ingest",
                     "artifact": "rounds/round-001/generator/ingest-report.json",
                     "sha256": "b" * 64}],
        "evidence_index": {}, "scope_statement": "full rebuild"})
    (case / "report" / "v1-cand-012.md").parent.mkdir(parents=True,
                                                      exist_ok=True)
    (case / "report" / "v1-cand-012.md").write_text(
        "# INTC update\n\n## Summary\n"
        "Revenue rose to 13,500 [EV:income_statement.revenue] with an "
        "improved gross margin.\n\n## Results\nEBIT up on cost discipline.\n"
        "\n### Segment detail\nCCG leads the recovery.\n"
        "\n## Outlook\nFoundry ramp supports the guide.\n"
        "\n## Valuation\nDCF 14.40 [EV:dcf.per_diluted_share] per share.\n"
        "\n## Risks\nExecution risk on 18A; China exposure.\n",
        encoding="utf-8")
    _write(case / "approvals" / "apr-0001.json", {
        "decision_id": "apr-0001", "case_id": CUT,
        "decision": "APPROVED", "by": "Gaetano Marco Chiantia",
        "ts": "2026-11-09T17:00:00Z", "reasoning": "verified"})
    return case


class TestRenderDossier:
    def test_collect_and_fill_all_tokens(self, tmp_path: Path):
        case = make_mini_case(tmp_path)
        ev = collect_case(case)
        assert ev["report_version"] == "v1-cand-012"
        tex = render_dossier_tex(ev)
        import re
        assert not re.findall(r"\{\{[A-Z_0-9]+\}\}", tex)
        # key numbers verbatim from the evidence artifacts
        assert "13,500" in tex          # revenue 2026-Q3 (mm)
        assert "14.40" in tex           # DCF per share, exit multiple (bridge exhibit)
        assert "intc-10q-2026q3" in tex          # input table row
        assert "aaaaaaaaaaaa" in tex             # manifest sha (first 12 characters)
        assert "Gaetano Marco Chiantia" in tex   # approver from the decision record
        assert "2026-10-30" in tex or "30 Oct 2026" in tex
        assert "\\drafttrue" not in tex          # approved case: no watermark

    def test_build_writes_tex_without_pdf_when_no_latex(self, tmp_path,
                                                        monkeypatch):
        case = make_mini_case(tmp_path)
        monkeypatch.setattr("shutil.which", lambda name: None)
        out = build_dossier(case, out_dir=tmp_path / "out",
                            compile_pdf=True)
        assert out["compiled"] is False
        assert Path(out["tex"]).exists()
        assert not (tmp_path / "out" / "dossier.pdf").exists()

    def test_section_fallback_when_heading_missing(self, tmp_path):
        case = make_mini_case(tmp_path)
        (case / "report" / "v1-cand-012.md").write_text(
            "## Summary\nOnly one section.\n", encoding="utf-8")
        tex = render_dossier_tex(collect_case(case))
        assert "Only one section." in tex


class TestPdfCompile:
    def test_pdf_compiles_when_pdflatex_available(self, tmp_path):
        if not shutil.which("pdflatex"):
            pytest.skip("pdflatex not installed")
        case = make_mini_case(tmp_path)
        out = build_dossier(case)
        assert out["compiled"] is True
        pdf = Path(out["pdf"])
        assert pdf.exists() and pdf.read_bytes()[:5] == b"%PDF-"
        # the compiled PDF has multiple pages of content
        assert pdf.stat().st_size > 10_000

    def test_cli_renders_to_case_export_dir(self, tmp_path):
        case = make_mini_case(tmp_path)
        proc = subprocess.run(
            [sys.executable, "-m", "coverageflow.export_dossier", "--no-pdf",
             "--case", str(case)],
            capture_output=True, text=True, cwd=str(Path(__file__).parent.parent))
        assert proc.returncode == 0, proc.stderr
        # exports carry the case id, the version and the document kind
        texs = list((case / "export").glob("*-dossier*.tex"))
        assert len(texs) == 1, texs
        assert texs[0].name.startswith(case.name + "-")
