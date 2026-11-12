"""Build the research note (PDF and HTML) from a collected case.

One entry point, :func:`build_note`, shared by the CLI (``report_doc``), the
dossier export (``export_dossier.build_dossier``) and the UI export
(``ui.service.export_case``), so all of them produce identical output.

Charts need matplotlib; without it the note is rendered without figures.
PDF compilation needs ``pdflatex`` on PATH; without it the ``.tex`` is the
deliverable (same contract as the old dossier export).
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

from .note_charts import make_charts
from .render_note_html import render_note_html
from .render_note_tex import render_note_tex
from .report_doc import build_report_doc


def assert_no_em_dash(*paths: Path) -> None:
    """Fail loudly if U+2014 (or the TeX '---' ligature) reaches an output."""
    for p in paths:
        t = Path(p).read_text(encoding="utf-8")
        body = re.sub(r"(?m)^%.*$", "", t) if str(p).endswith(".tex") else t
        if "—" in t or "―" in t or (str(p).endswith(".tex") and "---" in body):
            raise AssertionError(f"em dash found in {p}")


def _compile(tex: Path) -> tuple[bool, list[str]]:
    """pdflatex twice (page count for 'n / N'); returns (ok, log warnings)."""
    proc = None
    for _ in range(2):
        proc = subprocess.run(["pdflatex", "-interaction=nonstopmode", "-halt-on-error",
                               "-output-directory", str(tex.parent), str(tex)],
                              capture_output=True, text=True, timeout=240, cwd=str(tex.parent))
        if proc.returncode != 0:
            break
    log = tex.with_suffix(".log")
    text = log.read_text(errors="replace") if log.exists() else ""
    warn = [ln for ln in text.splitlines() if re.search(r"Overfull|Underfull \\vbox|^! ", ln)]
    ok = bool(proc) and proc.returncode == 0 and tex.with_suffix(".pdf").exists()
    if not ok:
        (tex.parent / f"{tex.stem}_pdflatex.log").write_text(
            (proc.stdout[-4000:] if proc else "") + "\n---STDERR---\n" + (proc.stderr[-2000:] if proc else ""),
            encoding="utf-8")
    return ok, warn


def build_note(evidence: dict, out_dir: Path, stem: str = "dossier", html_name: str | None = None,
               force_draft: bool = False, status: str | None = None, cover_only: bool = False,
               compile_pdf: bool = True, with_html: bool = True) -> dict:
    """Render ``<stem>.tex`` (+ ``.pdf`` when pdflatex exists) and optionally an HTML file.

    ``status`` is the case status (CLOSED, PENDING_APPROVAL, ESCALATED, ...); anything that
    is not a final status renders as a draft with a watermark on every page.
    """
    out_dir = Path(out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    doc = build_report_doc(evidence, force_draft=force_draft, status=status)
    charts = make_charts(doc)
    figs: dict[str, str] = {}
    if charts:
        fig_dir = out_dir / f"{stem}-fig"
        fig_dir.mkdir(exist_ok=True)
        for name, (_svg, pdf) in charts.items():
            (fig_dir / f"{name}.pdf").write_bytes(pdf)
            figs[name] = f"{stem}-fig/{name}.pdf"
    tex = out_dir / f"{stem}.tex"
    tex.write_text(render_note_tex(doc, figs, cover_only=cover_only), encoding="utf-8")
    assert_no_em_dash(tex)
    res: dict = {"tex": str(tex), "pdf": None, "compiled": False, "html": None, "warnings": [], "doc": doc}
    if compile_pdf and shutil.which("pdflatex"):
        ok, warn = _compile(tex)
        res["compiled"], res["warnings"] = ok, warn
        res["pdf"] = str(tex.with_suffix(".pdf")) if ok else None
    if with_html and html_name:
        html = out_dir / html_name
        html.write_text(render_note_html(doc, {k: v[0] for k, v in charts.items()}, cover_only=cover_only),
                        encoding="utf-8")
        assert_no_em_dash(html)
        res["html"] = str(html)
    return res


def build_html(evidence: dict, html_path: Path, status: str | None = None, force_draft: bool = False,
               cover_only: bool = False) -> dict:
    """Render only the self-contained HTML note (no TeX, no PDF) to ``html_path``."""
    html_path = Path(html_path)
    html_path.parent.mkdir(parents=True, exist_ok=True)
    doc = build_report_doc(evidence, force_draft=force_draft, status=status)
    charts = make_charts(doc)
    html_path.write_text(render_note_html(doc, {k: v[0] for k, v in charts.items()}, cover_only=cover_only),
                         encoding="utf-8")
    assert_no_em_dash(html_path)
    return {"html": str(html_path), "doc": doc}
