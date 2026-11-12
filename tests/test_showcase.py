"""The shipped showcase case: installed once, filings restored byte-identical."""

import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import launch  # noqa: E402

CASE = "amd-fy2026q3r3-20261104"


def test_showcase_is_installed_with_verified_filings(tmp_path):
    ws = tmp_path / "ws"
    (ws / "cases").mkdir(parents=True)
    launch.install_showcase(ws)
    case = ws / "cases" / CASE
    state = json.loads((case / "state.json").read_text())
    assert state["status"] == "CLOSED"
    man = json.loads((case / "inputs" / "manifest.json").read_text())
    for doc in man["documents"]:
        data = (case / doc["path"]).read_bytes()
        assert hashlib.sha256(data).hexdigest() == doc["sha256"]
    # the exported note ships with the case
    assert list((case / "export").glob(f"{CASE}-*-dossier.pdf"))


def test_showcase_never_overwrites_an_existing_case(tmp_path):
    ws = tmp_path / "ws"
    (ws / "cases" / CASE).mkdir(parents=True)
    marker = ws / "cases" / CASE / "mine.txt"
    marker.write_text("keep")
    launch.install_showcase(ws)
    assert marker.read_text() == "keep"
    assert not (ws / "cases" / CASE / "state.json").exists()


def test_showcase_is_stored_without_the_filings():
    stored = ROOT / "examples" / "showcase" / "cases" / CASE / "inputs"
    assert not list(stored.glob("*.htm")), "filings belong in data/, not twice"
