"""render_clean: reader-facing note, marked record untouched (5.3)."""

from __future__ import annotations

from pathlib import Path

from coverageflow.export_dossier import (build_dossier, collect_case,
                                          render_dossier_tex)
from coverageflow.render_note import render_clean, strip_process_sections
from tests.test_export_dossier import make_mini_case

MD = """# Intel Update

> **Cutoff:** 2026-10-30 · **Bundle:** b1
> **Candidate:** cand-1 · **Request:** req-9

## Summary

Revenue of $16.5bn [EV:income_statement.revenue] grew 20% [EV:derived.revenue_yoy][EV:income_statement.revenue].

| a | b |
|---|---|
| 1 [EV:income_statement.diluted_eps] | 2 |

## Open findings

Carried finding, per-role token cap: deferred. harness dispatch-accounting.

## Risks

Execution risk.

The harness budget was exhausted in round 3.

### Finding dispositions

closed.
"""


def test_markers_become_numbered_footnotes_with_sources():
    idx = {"income_statement.revenue": {"doc_id": "10q", "locator": "line 14"},
           "derived.revenue_yoy": "generator/model.json"}
    out = render_clean(MD, idx)
    assert "[EV:" not in out
    assert "$16.5bn[^1]" in out
    assert "20%[^2][^1]" in out            # same key reuses its number
    assert "[^1]: `income_statement.revenue` (10q, line 14)" in out
    assert "[^2]: `derived.revenue_yoy`" in out
    assert "generator/model.json" not in out    # internal path not shown
    assert "[^3]: `income_statement.diluted_eps`" in out


def test_process_sections_and_text_removed():
    out = render_clean(MD, {})
    for bad in ("Open findings", "token cap", "harness", "Finding dispositions",
                "dispositions"):
        assert bad not in out
    assert "## Risks" in out and "Execution risk." in out


def test_header_moves_to_provenance_footer():
    out = render_clean(MD, {})
    body, _, footer = out.partition("*Provenance:")
    assert "Candidate" not in body and "Request" not in body
    assert "cand-1" in footer and "req-9" in footer and "2026-10-30" in footer
    assert out.startswith("# Intel Update")


def test_exceptions_notice_stays_visible():
    md = "# T\n\n> **PUBLISHED WITH EXCEPTIONS.** 2 open\n> **Candidate:** c\n\n## S\n\nx\n"
    out = render_clean(md, {})
    assert "PUBLISHED WITH EXCEPTIONS" in out


def test_input_not_mutated_and_strip_keeps_markers():
    kept = strip_process_sections(MD)
    assert "[EV:income_statement.revenue]" in kept
    assert "Open findings" not in kept
    assert "[EV:" in MD


def test_dossier_export_writes_clean_note_and_drops_process(tmp_path: Path, monkeypatch):
    case = make_mini_case(tmp_path)
    rep = case / "report" / "v1-cand-012.md"
    rep.write_text(rep.read_text("utf-8") + "\n## Open findings\n\nharness text zzz\n",
                   "utf-8")
    ev = collect_case(case)
    assert "zzz" in ev["report_md"]
    assert "zzz" not in render_dossier_tex(ev)
    monkeypatch.setattr("shutil.which", lambda name: None)
    out = build_dossier(case, out_dir=tmp_path / "out", compile_pdf=False)
    clean = Path(out["clean_note"]).read_text("utf-8")
    assert "[EV:" not in clean and "zzz" not in clean
    assert "zzz" in rep.read_text("utf-8")      # the record is untouched
