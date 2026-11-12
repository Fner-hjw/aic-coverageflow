"""Writer card 1.4: units, no process text, changes-since-last-note rule."""

from __future__ import annotations

import re

from coverageflow import _prompt_assembly as pa

ROOT = pa.default_root()
CARD = ROOT / "prompts" / "writer.md"


def test_version_bumped_and_example_matches():
    text = CARD.read_text("utf-8")
    ver = pa.parse_role_card(CARD)["version"]
    assert ver == "1.5"
    assert set(re.findall(r'prompt_version\("([^"]+)"\)', text)) == {ver}
    assert set(re.findall(r'"prompt_version":"([^"]+)"', text)) == {ver}


def test_card_requires_units_forbids_process_text_and_has_changes_rule():
    text = CARD.read_text("utf-8")
    assert "Units on every number in body text" in text and "$13.7bn" in text
    assert "No process text in the note" in text
    assert "Open findings" in text            # named as forbidden
    assert "## Changes since last note" in text
    assert "`prior_note`" in text
    assert "old versus new target" in text
    assert "estimate changes" in text
