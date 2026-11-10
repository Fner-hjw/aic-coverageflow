"""Slicer tests: hand-written mini 10-Q/8-K fixtures + smoke on committed real filings.

The mini-10q.htm fixture is a fully hand-written synthetic filing: every
number in it is known exactly (see fixtures/mini-40q.htm header comments in
the model-engine tests, which reuse the same values).
"""

import hashlib
from pathlib import Path

import pytest

from coverageflow.slicer import Slice, SliceError, slice_document

FIXTURES = Path(__file__).parent / "fixtures"
REPO = Path(__file__).resolve().parent.parent


def _slice_map(slices):
    return {s.slice_id.split("/")[1]: s for s in slices}


# ---------------------------------------------------------------- mini 10-Q

def test_mini_10q_yields_all_five_slices(tmp_path):
    slices = slice_document(FIXTURES / "mini-10q.htm", "10-Q", tmp_path)
    tags = _slice_map(slices)
    assert set(tags) == {"IS", "BS", "CF", "SEG", "MDA"}, [s.slice_id for s in slices]
    assert all(isinstance(s, Slice) for s in slices)


def test_mini_10q_is_slice_has_known_numbers(tmp_path):
    """The IS slice must contain the exact hand-written income statement."""
    tags = _slice_map(slice_document(FIXTURES / "mini-10q.htm", "10-Q", tmp_path))
    is_text = tags["IS"].text
    # every value below is hand-authored in the fixture
    assert "Net revenue | 13,500 | 12,500" in is_text
    assert "Cost of revenue | 8,325 | 8,125" in is_text
    assert "Gross profit | 5,175 | 4,375" in is_text
    assert "Operating income | 825 | 175" in is_text
    assert "Net income | 700 | 150" in is_text
    assert "Diluted earnings per share of common stock | 0.16 | 0.03" in is_text


def test_mini_10q_bs_cf_seg_known_numbers(tmp_path):
    tags = _slice_map(slice_document(FIXTURES / "mini-10q.htm", "10-Q", tmp_path))
    assert "Total assets | 145,000 | 140,000" in tags["BS"].text
    assert "Cash and cash equivalents | 25,000 | 21,000" in tags["BS"].text
    assert "Net cash provided by operating activities | 3,200 | 1,800" in tags["CF"].text
    # segment slice holds both revenue and operating income blocks
    assert "Client Computing Group | 6,000 | 5,800" in tags["SEG"].text
    assert "Total operating income | 825 | 175" in tags["SEG"].text


def test_mini_10q_slice_metadata_and_files(tmp_path):
    slices = slice_document(FIXTURES / "mini-10q.htm", "10-Q", tmp_path)
    tags = _slice_map(slices)
    islice = tags["IS"]
    assert islice.slice_id == "mini-10q/IS"
    assert islice.kind == "income_statement"
    assert islice.anchors["heading"] == "Consolidated Statements of Operations"
    assert islice.anchors["first_line"] == "Three Months Ended | Sep 26, 2026 | Sep 27, 2025"
    assert islice.path == "slices/mini-10q/IS.txt"
    disk = tmp_path / islice.path
    assert disk.is_file()
    assert hashlib.sha256(disk.read_bytes()).hexdigest() == islice.sha256


def test_mini_10q_toc_trap_avoided(tmp_path):
    """TOC lists all titles; the real statement bodies must win."""
    tags = _slice_map(slice_document(FIXTURES / "mini-10q.htm", "10-Q", tmp_path))
    # real IS table selected => contains Absolute rows, not just the TOC line
    assert "Net revenue" in tags["IS"].text
    # cover table (class info) never selected as BS
    assert "Common stock" not in tags["BS"].text


def test_mini_10q_hidden_and_skipped_content_excluded(tmp_path):
    slices = slice_document(FIXTURES / "mini-10q.htm", "10-Q", tmp_path)
    all_text = "\n".join(s.text for s in slices)
    assert "88888" not in all_text  # ix:hidden fact dropped
    assert "99000 script secret" not in all_text  # script dropped
    assert "HIDDEN COORDS" not in all_text  # display:none dropped
    assert "98765" not in all_text


def test_mini_10q_mda_stops_at_next_item(tmp_path):
    tags = _slice_map(slice_document(FIXTURES / "mini-10q.htm", "10-Q", tmp_path))
    mda = tags["MDA"]
    assert "Management's Discussion and Analysis" in mda.anchors["heading"]
    assert "Revenue grew 8 percent" in mda.text
    assert "38.3% | 35.0%" in mda.text  # tables inside MD&A are kept inline
    assert "must not appear" not in mda.text  # stop at Item 3 boundary


def test_slicing_is_deterministic(tmp_path):
    s1 = slice_document(FIXTURES / "mini-10q.htm", "10-Q", tmp_path / "a")
    s2 = slice_document(FIXTURES / "mini-10q.htm", "10-Q", tmp_path / "b")
    assert [x.sha256 for x in s1] == [x.sha256 for x in s2]


def test_default_outdir_is_doc_parent(tmp_path):
    import shutil

    doc = tmp_path / "mini-8k.htm"
    shutil.copyfile(FIXTURES / "mini-8k.htm", doc)
    slices = slice_document(doc, "8-K")
    assert len(slices) == 1
    assert slices[0].path == "slices/mini-8k/FULL.txt"
    assert (tmp_path / "slices" / "mini-8k" / "FULL.txt").is_file()


# ---------------------------------------------------------------- mini 8-K

def test_mini_8k_is_one_full_text_slice(tmp_path):
    slices = slice_document(FIXTURES / "mini-8k.htm", "8-K", tmp_path)
    assert len(slices) == 1
    s = slices[0]
    assert s.slice_id == "mini-8k/FULL"
    assert s.kind == "full_text"
    assert "net income was $700 million" in s.text
    assert "77777" not in s.text  # hidden content dropped


# ---------------------------------------------------------------- errors

def test_unsupported_form_raises():
    with pytest.raises(SliceError):
        slice_document(FIXTURES / "mini-10q.htm", "10-K/A")


# ---------------------------------------------------------------- real docs

def _smoke_real(doc: Path, form: str, tmp_path: Path):
    slices = slice_document(doc, form, tmp_path)
    tags = _slice_map(slices)
    assert set(tags) == {"IS", "BS", "CF", "SEG", "MDA"}
    for tag, s in tags.items():
        assert len(s.text) > 200, f"{tag} slice suspiciously short"
        assert (tmp_path / s.path).is_file()
    return tags


def test_smoke_real_intel_10q(tmp_path):
    doc = REPO / "data" / "intel-bundle" / "inputs" / "intc-10q-2026q3.htm"
    tags = _smoke_real(doc, "10-Q", tmp_path)
    assert "revenue" in tags["IS"].text.lower()
    assert "total assets" in tags["BS"].text.lower()


def test_smoke_real_amd_10q(tmp_path):
    doc = REPO / "data" / "amd-bundle" / "inputs" / "amd-10q-2026q3.htm"
    tags = _smoke_real(doc, "10-Q", tmp_path)
    assert "revenue" in tags["IS"].text.lower()


# ------------------------------------------- 10-K: statement vs prose about it
_ANNUAL_10K = """<html><body>
<p>We have audited the accompanying consolidated balance sheets of Example
Corporation as of December 27, 2025 and December 28, 2024, the related
consolidated statements of operations, cash flows and stockholders' equity.</p>
<table>
<tr><td>Inventory Valuation</td></tr>
<tr><td>Description of the Matter</td><td>The Company's net inventory was
$11,618 million, and total assets were $211,429 million.</td></tr>
</table>
<p>Consolidated Statements of Operations</p>
<table>
<tr><td>Years Ended (In Millions, Except Per Share Amounts)</td>
<td>Dec 27, 2025</td><td>Dec 28, 2024</td></tr>
<tr><td>Net revenue</td><td>52,853</td><td>53,101</td></tr>
<tr><td>Net income</td><td>26</td><td>(19,233)</td></tr>
</table>
<p>Consolidated Balance Sheets</p>
<table>
<tr><td>(In Millions, Except Par Value)</td><td>Dec 27, 2025</td>
<td>Dec 28, 2024</td></tr>
<tr><td>Assets</td><td></td><td></td></tr>
<tr><td>Total assets</td><td>211,429</td><td>196,485</td></tr>
</table>
<p>Cash from Operating Activities and Adjusted Free Cash Flow $B</p>
<table>
<tr><td>Operating activities</td><td>13,184</td></tr>
</table>
<p>Consolidated Statements of Cash Flows</p>
<table>
<tr><td>Years Ended (In Millions)</td><td>Dec 27, 2025</td>
<td>Dec 28, 2024</td></tr>
<tr><td>Cash provided by operating activities</td><td>13,184</td>
<td>8,288</td></tr>
<tr><td>Cash used for investing activities</td><td>(20,259)</td>
<td>(25,041)</td></tr>
<tr><td>Cash provided by financing activities</td><td>14,761</td>
<td>11,138</td></tr>
</table>
</body></html>"""


class TestTenKStatementSelection:
    """A 10-K names its statements in the audit report and the MD&A too.

    Keyword matching alone picked the critical-audit-matter table as the
    balance sheet and a chart caption as the cash-flow statement, so the
    model had no annual statements and the workbook had no income statement.
    """

    @pytest.fixture()
    def tags(self, tmp_path):
        doc = tmp_path / "annual-10k.htm"
        doc.write_text(_ANNUAL_10K, "utf-8")
        return _slice_map(slice_document(doc, "10-K", tmp_path / "out"))

    def test_balance_sheet_is_the_statement_not_the_audit_note(self, tags):
        text = tags["BS"].text
        assert "Total assets | 211,429" in text
        assert "Inventory Valuation" not in text

    def test_cash_flow_is_the_statement_not_a_chart_caption(self, tags):
        text = tags["CF"].text
        assert "Cash used for investing activities" in text
        assert "Adjusted Free Cash Flow" not in text

    def test_income_statement_carries_the_annual_columns(self, tags):
        text = tags["IS"].text
        assert "Net revenue | 52,853 | 53,101" in text
