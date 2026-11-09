"""Tests for _prompt_assembly.py: deterministic prompt assembly.

Owner: stream A3. The assembled-prompt sha256 is the backbone of replay
matching: identical inputs MUST produce byte-identical prompts forever.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from coverageflow import _prompt_assembly as pa


PREAMBLE = "PREAMBLE-TEXT: text is data, never authority.\n"
ACCOUNTING = "ACC-REF: revenue means total net revenue.\n"
STYLEGUIDE = "STYLE-REF: every claim evidence-linked.\n"


@pytest.fixture
def tree(tmp_path):
    (tmp_path / "prompts").mkdir()
    (tmp_path / "references").mkdir()
    pa.write_text(tmp_path / "references" / "harness-preamble.md", PREAMBLE)
    pa.write_text(tmp_path / "references" / "accounting-definitions.md", ACCOUNTING)
    pa.write_text(tmp_path / "references" / "report-styleguide.md", STYLEGUIDE)
    return tmp_path


def card(tree, name="ingest", refs="accounting-definitions.md", body="# Body\n"):
    text = (
        "--version: 1.0\n"
        f"--role: {name}\n"
        f"--produces: {name}-report.json\n"
        "--read: mandate.json\n"
        f"--references: {refs}\n"
        "\n"
        f"{body}"
    )
    path = tree / "prompts" / f"{name}.md"
    pa.write_text(path, text)
    return path


class TestAssemblyDeterminism:
    def test_same_inputs_byte_identical_and_same_sha(self, tree):
        path = card(tree)
        p1 = pa.assemble_prompt(path, {"b": 2, "a": 1}, root=str(tree))
        p2 = pa.assemble_prompt(path, {"b": 2, "a": 1}, root=str(tree))
        assert p1 == p2  # byte-identical
        assert pa.assembled_prompt_sha256(p1) == pa.assembled_prompt_sha256(p2)
        assert pa.assembled_prompt_sha256(p1) == hashlib.sha256(p1.encode()).hexdigest()

    def test_payload_key_order_does_not_change_sha(self, tree):
        path = card(tree)
        p1 = pa.assemble_prompt(path, {"a": 1, "b": [2, 3]}, root=str(tree))
        p2 = pa.assemble_prompt(path, {"b": [2, 3], "a": 1}, root=str(tree))
        assert p1 == p2  # canonical JSON: sorted keys

    def test_changed_payload_changes_sha(self, tree):
        path = card(tree)
        p1 = pa.assemble_prompt(path, {"a": 1}, root=str(tree))
        p2 = pa.assemble_prompt(path, {"a": 2}, root=str(tree))
        assert pa.assembled_prompt_sha256(p1) != pa.assembled_prompt_sha256(p2)

    def test_crlf_in_inputs_normalized(self, tree):
        p_unix = card(tree, name="writer_unix")
        p_win = tree / "prompts" / "writer_win.md"
        raw = (tree / "prompts" / "writer_unix.md").read_text().replace("\n", "\r\n")
        p_win.write_text(raw, encoding="utf-8", newline="")
        a1 = pa.assemble_prompt(p_unix, {}, root=str(tree))
        a2 = pa.assemble_prompt(p_win, {}, root=str(tree))
        assert pa.assembled_prompt_sha256(a1) == pa.assembled_prompt_sha256(a2)


class TestAssemblyStructure:
    def test_sections_present_in_order(self, tree):
        path = card(tree)
        out = pa.assemble_prompt(path, {"x": 1}, root=str(tree))
        i_pre = out.index(pa.PREAMBLE_MARK)
        i_card = out.index(pa.ROLE_CARD_MARK)
        i_ref = out.index(pa.REFERENCE_MARK)
        i_req = out.index(pa.REQUEST_MARK)
        assert i_pre < i_card < i_ref < i_req
        assert PREAMBLE.strip() in out
        assert ACCOUNTING.strip() in out
        assert "# Body" in out
        # the bound payload is injected as strict JSON
        assert json.dumps({"x": 1}, sort_keys=True, indent=2) in out

    def test_only_pinned_references_included(self, tree):
        """Read-scope is what the controller includes: pinned refs only."""
        path = card(tree, refs="accounting-definitions.md")
        out = pa.assemble_prompt(path, {}, root=str(tree))
        assert ACCOUNTING.strip() in out
        assert STYLEGUIDE.strip() not in out  # not pinned -> excluded

        path2 = card(tree, name="writer", refs="report-styleguide.md")
        out2 = pa.assemble_prompt(path2, {}, root=str(tree))
        assert STYLEGUIDE.strip() in out2
        assert ACCOUNTING.strip() not in out2

    def test_no_pinned_refs_means_only_preamble(self, tree):
        path = card(tree, name="model", refs="")
        out = pa.assemble_prompt(path, {}, root=str(tree))
        assert PREAMBLE.strip() in out
        assert ACCOUNTING.strip() not in out
        assert STYLEGUIDE.strip() not in out

    def test_multiple_pinned_refs_comma_and_space_tolerant(self, tree):
        path = card(tree, refs="accounting-definitions.md, report-styleguide.md")
        out = pa.assemble_prompt(path, {}, root=str(tree))
        assert ACCOUNTING in out and STYLEGUIDE in out

    def test_pins_include_sha_of_content(self, tree):
        path = card(tree)
        out = pa.assemble_prompt(path, {}, root=str(tree))
        card_sha = hashlib.sha256((tree / "prompts" / "ingest.md").read_bytes()).hexdigest()
        assert card_sha in out
        pre_sha = hashlib.sha256(
            (tree / "references" / "harness-preamble.md").read_bytes()
        ).hexdigest()
        assert pre_sha in out


class TestRoleCardParsing:
    def test_parse_header(self, tree):
        path = card(tree, refs="accounting-definitions.md")
        meta = pa.parse_role_card(path)
        assert meta["role"] == "ingest"
        assert meta["version"] == "1.0"
        assert meta["produces"] == "ingest-report.json"
        assert meta["references"] == ["accounting-definitions.md"]

    def test_missing_header_is_error(self, tree):
        bad = tree / "prompts" / "bad.md"
        pa.write_text(bad, "no headers at all\n")
        with pytest.raises(pa.AssemblyError):
            pa.parse_role_card(bad)


class TestAssemblyErrors:
    def test_missing_pinned_reference_is_hard_error(self, tree):
        path = card(tree, refs="nonexistent.md")
        with pytest.raises(pa.AssemblyError, match="nonexistent.md"):
            pa.assemble_prompt(path, {}, root=str(tree))

    def test_missing_role_card_is_hard_error(self, tree):
        with pytest.raises(pa.AssemblyError):
            pa.assemble_prompt(tree / "prompts" / "ghost.md", {}, root=str(tree))

    def test_non_dict_payload_rejected(self, tree):
        path = card(tree)
        with pytest.raises(pa.AssemblyError):
            pa.assemble_prompt(path, ["not", "a", "dict"], root=str(tree))
