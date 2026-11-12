"""Research-note export (owner: stream A5; contracts interfaces section 4).

Turns a case's VERIFIED artifacts into the CoverageFlow research note:

  report.md (writer role, evidence-linked)
    + model.json / valuation.json (deterministic engines)
    + case metadata (mandate, result, input manifest, approval records)
    -> report_doc (structured dict; coverageflow/report_doc.py)
    -> references/note-template.tex via render_note_tex -> pdflatex
    -> <case>/export/<case_id>-<version>-dossier.{tex,pdf}   (+ -note.clean.md)

The HTML export (ui.service.export_case) renders the same report_doc with
render_note_html.  All numbers are read from the evidence artifacts (nothing
is hand-typed), so the note cannot drift from the verified record.  If
pdflatex is not on PATH the .tex is left on disk (graceful fallback).

CLI:   python -m coverageflow.export_dossier --case <case-dir> [--out-dir export]
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .render_note import render_clean

TEMPLATE_PATH = Path(__file__).resolve().parent.parent / "references" / "note-template.tex"


# ----------------------------------------------------------------- helpers
def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _read_json_or_none(path: Path) -> dict | None:
    try:
        return _read_json(path)
    except (OSError, json.JSONDecodeError):
        return None


def collect_case(case_dir: Path) -> dict:
    """Load the verified artifacts of a case directory into an evidence dict."""
    case_dir = Path(case_dir)
    mandate = _read_json(case_dir / "mandate.json")
    result = _read_json_or_none(case_dir / "result.json")
    state = _read_json_or_none(case_dir / "state.json") or {}
    manifest = _read_json_or_none(case_dir / "inputs" / "manifest.json")

    # candidate round: prefer the closed result, else the last built round
    rounds_dir = case_dir / "rounds"
    built_round = (state.get("candidate") or {}).get("built_round") or 1
    round_dir = rounds_dir / f"round-{built_round:03d}"
    gen_dir = round_dir / "generator"

    report_version = None
    report_md = ""
    if result:
        report_version = result.get("report_version")
        report_path = case_dir / "report" / f"{report_version}.md"
        if report_path.exists():
            report_md = report_path.read_text(encoding="utf-8")
        else:
            for p in sorted(q for q in (case_dir / "report").glob("*.md")
                            if not q.name.endswith(".clean.md")) if (case_dir / "report").exists() else []:
                report_md = p.read_text(encoding="utf-8")
    if not report_md:
        fallback = gen_dir / "report.md"
        if fallback.exists():
            report_md = fallback.read_text(encoding="utf-8")

    approval = None
    appr_dir = case_dir / "approvals"
    if appr_dir.exists():
        for p in sorted(appr_dir.glob("*.json")):
            approval = _read_json(p)
    candidate_manifest = _read_json_or_none(gen_dir / "candidate-manifest.json")
    return {
        "case_dir": case_dir,
        "case_id": (state.get("case_id") or case_dir.name),
        "mandate": mandate,
        "result": result,
        "state": state,
        "manifest": manifest,
        "manifest_path": case_dir / "inputs" / "manifest.json",
        "facts": _read_json_or_none(gen_dir / "facts.json") or {},
        "model": _read_json_or_none(gen_dir / "model.json") or {},
        "valuation": _read_json_or_none(gen_dir / "valuation.json") or {},
        "report_md": report_md,
        "report_version": report_version,
        "candidate_manifest": candidate_manifest,
        "approval": approval,
        "built_round": built_round,
    }


def render_dossier_tex(evidence: dict, template_path: Path | None = None, status: str | None = None,
                       force_draft: bool = False) -> str:
    """The note as LaTeX source (charts omitted: no figure files are written here)."""
    from .render_note_tex import render_note_tex
    from .report_doc import build_report_doc

    doc = build_report_doc(evidence, force_draft=force_draft, status=status)
    return render_note_tex(doc, {})


def evidence_index_from_facts(evidence: dict) -> dict[str, dict]:
    """key -> {doc_id, anchor, locator} from the candidate's cited facts."""
    idx: dict[str, dict] = {}
    for fact in (evidence.get("facts") or {}).get("facts") or []:
        cit = fact.get("citation") or {}
        if fact.get("field") and fact["field"] not in idx:
            idx[fact["field"]] = {k: cit[k] for k in ("doc_id", "anchor", "locator")
                                  if cit.get(k)}
    return idx


def write_clean_note(evidence: dict, out_path: Path) -> Path:
    """Write the reader-facing note (footnotes, no process text) to *out_path*.

    The marked report in report/ remains the verified record.
    """
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        render_clean(evidence.get("report_md") or "",
                     evidence_index_from_facts(evidence)), encoding="utf-8")
    return out_path


FINAL_STATUSES = ("CLOSED", "APPROVED", "PUBLISHED_WITH_EXCEPTIONS")


def export_stem(case_id: str, version: str | None, kind: str,
                draft: bool = False) -> str:
    """File stem every export shares: ``<case_id>-<version>-<kind>[-DRAFT]``.

    ``kind`` names the document (``dossier`` for the note as PDF/HTML/TeX,
    ``model`` for the workbook, ``note`` for the clean markdown), so a file
    taken out of the case folder still says which case, which published
    version and what it is. Unapproved exports carry ``-DRAFT``.
    """
    ver = version or "draft"
    tail = "-DRAFT" if draft and ver != "draft" else ""
    return f"{case_id}-{ver}-{kind}{tail}"


def export_filename(case_id: str, version: str | None, kind: str, ext: str,
                    draft: bool = False) -> str:
    return f"{export_stem(case_id, version, kind, draft)}.{ext}"


def build_dossier(case_dir: Path, out_dir: Path | None = None,
                  compile_pdf: bool = True, version: str | None = None,
                  draft: bool | None = None) -> dict:
    """Render + compile; returns written paths and compilation status.

    Files are named ``<case_id>-<version>-dossier.{tex,pdf}`` (plus
    ``-note.clean.md``); ``version``/``draft`` default to the case's
    published version and status.
    """
    from .note_build import build_note

    evidence = collect_case(Path(case_dir))
    out_dir = Path(out_dir) if out_dir else evidence["case_dir"] / "export"
    out_dir.mkdir(parents=True, exist_ok=True)
    version = version or evidence["report_version"]
    if draft is None:
        draft = (evidence.get("state") or {}).get("status") not in FINAL_STATUSES
    case_id = evidence["case_id"]
    stem = export_stem(case_id, version, "dossier", draft)
    built = build_note(evidence, out_dir, stem=stem, compile_pdf=compile_pdf,
                       with_html=False)
    clean_path = write_clean_note(
        evidence, out_dir / export_filename(case_id, version, "note", "clean.md", draft))
    return {"evidence_case": str(evidence["case_dir"]), "tex": built["tex"],
            "clean_note": str(clean_path),
            "pdf": built["pdf"], "compiled": built["compiled"],
            "report_version": evidence["report_version"]}


# -------------------------------------------------------------- contract
def export_dossier_pdf(report_md: Path, evidence: dict, out: Path) -> Path:
    """Contract entry (interfaces section 4): render + compile to *out*.

    evidence keys: model, valuation, mandate, manifest_path, candidate_manifest,
    approval, state, result (as produced by collect_case).
    """
    from .note_build import build_note

    ev = dict(evidence)
    ev["report_md"] = Path(report_md).read_text(encoding="utf-8")
    out = Path(out)
    built = build_note(ev, out.parent, stem=out.stem, with_html=False)
    if built["pdf"] and Path(built["pdf"]).exists():
        return Path(built["pdf"])
    # fallback: the .tex is the deliverable when pdflatex is unavailable
    return Path(built["tex"])


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--case", required=True, help="case directory "
                    "(e.g. cases/intc-fy2026q3-20261030)")
    ap.add_argument("--out-dir", default=None, help="output directory "
                    "(default: <case>/export)")
    ap.add_argument("--no-pdf", action="store_true",
                    help="render the -dossier.tex only (skip pdflatex)")
    args = ap.parse_args(argv)
    out = build_dossier(Path(args.case), out_dir=Path(args.out_dir)
                        if args.out_dir else None,
                        compile_pdf=not args.no_pdf)
    print(json.dumps(out, indent=2))
    return 0 if (out["pdf"] or not args.no_pdf) else 0


if __name__ == "__main__":
    raise SystemExit(main())
