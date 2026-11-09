"""Offline tests for coverageflow.bundle: manifest build via injected fetch."""

import hashlib
import json

import pytest

from coverageflow.bundle import (
    DocSpec,
    EdgarError,
    EdgarFetcher,
    Manifest,
    SpecMismatchError,
    build_bundle,
    load_manifest,
)


def _fake_edgar() -> "callable":
    """Return a fetch(url)->bytes callable serving a tiny synthetic EDGAR.

    CIK 9999 has two accessions; documents are tiny fixed byte strings.
    """
    submissions = {
        "cik": "9999",
        "recent": {
            "accessionNumber": [
                "0000000009-26-000001",
                "0000000009-26-000002",
            ],
            "form": ["10-Q", "8-K"],
            "filingDate": ["2026-04-25", "2026-03-02"],
            "reportDate": ["2026-03-28", "2026-03-02"],
            "primaryDocument": ["fake-20260328.htm", "fake8k.htm"],
        },
    }
    docs = {
        "https://www.sec.gov/Archives/edgar/data/9999/000000000926000001/fake-20260328.htm": b"<html>10Q</html>",
        "https://www.sec.gov/Archives/edgar/data/9999/000000000926000002/fake8k.htm": b"<html>8K</html>",
    }

    def fetch(url: str) -> bytes:
        if url == "https://data.sec.gov/submissions/CIK0000009999.json":
            return json.dumps(submissions).encode("utf-8")
        if url in docs:
            return docs[url]
        raise AssertionError(f"unexpected url {url}")

    return fetch


SPECS = [
    DocSpec(doc_id="fake-10q-2026q1", accession="0000000009-26-000001",
            form="10-Q", filed="2026-04-25", period="2026-03-28", ticker="FAKE"),
    DocSpec(doc_id="fake-8k-2026-03", accession="0000000009-26-000002",
            form="8-K", filed="2026-03-02", period="2026-03-02", ticker="FAKE"),
]


def test_build_bundle_writes_manifest_and_files(tmp_path):
    manifest = build_bundle("9999", SPECS, tmp_path, fetch=_fake_edgar())

    assert manifest.as_of_cutoff == "2026-04-25"
    assert manifest.bundle_id == "fake-2026-04"
    assert manifest.doc_ids == ["fake-10q-2026q1", "fake-8k-2026-03"]  # sorted by doc_id
    assert (tmp_path / "manifest.json").is_file()
    assert (tmp_path / "inputs/fake-10q-2026q1.htm").read_bytes() == b"<html>10Q</html>"
    doc = manifest.documents[0]
    assert doc.sha256 == hashlib.sha256(b"<html>10Q</html>").hexdigest()
    assert doc.bytes == len(b"<html>10Q</html>")
    assert doc.source_url.endswith("/fake-20260328.htm")
    assert doc.path == "inputs/fake-10q-2026q1.htm"


def test_manifest_json_matches_contract(tmp_path):
    manifest = build_bundle("9999", SPECS, tmp_path, fetch=_fake_edgar())
    data = json.loads((tmp_path / "manifest.json").read_text("utf-8"))
    assert set(data) == {"as_of_cutoff", "documents", "bundle_id", "bundle_sha256"}
    doc = data["documents"][0]
    assert set(doc) == {"doc_id", "form", "filed", "period", "path", "sha256",
                        "bytes", "source_url"}


def test_bundle_sha_is_ordered_doc_hashes(tmp_path):
    manifest = build_bundle("9999", SPECS, tmp_path, fetch=_fake_edgar())
    expected = hashlib.sha256(
        (manifest.documents[0].sha256 + manifest.documents[1].sha256).encode()
    ).hexdigest()
    assert manifest.bundle_sha256 == expected


def test_build_is_deterministic(tmp_path):
    m1 = build_manifest_with_verify(tmp_path / "a")
    m2 = build_manifest_with_verify(tmp_path / "b")
    assert m1.to_dict() == m2.to_dict()


def build_manifest_with_verify(dest):
    from pathlib import Path

    dest = Path(dest)
    manifest = build_bundle("9999", SPECS, dest, fetch=_fake_edgar())
    manifest.verify(dest)  # must not raise
    return manifest


def test_verify_detects_tampering(tmp_path):
    manifest = build_bundle("9999", SPECS, tmp_path, fetch=_fake_edgar())
    (tmp_path / "inputs" / "fake-8k-2026-03.htm").write_bytes(b"tampered")
    with pytest.raises(EdgarError, match="mismatch"):
        manifest.verify(tmp_path)


def test_spec_mismatch_raises(tmp_path):
    bad = [
        DocSpec(doc_id="fake-10q-2026q1", accession="0000000009-26-000001",
                form="10-K", ticker="FAKE"),
    ]
    with pytest.raises(SpecMismatchError, match="form"):
        build_bundle("9999", bad, tmp_path, fetch=_fake_edgar())


def test_missing_accession_raises(tmp_path):
    missing = [
        DocSpec(doc_id="nope-8k", accession="0000000009-26-000042", ticker="FAKE"),
    ]
    with pytest.raises(SpecMismatchError, match="not found"):
        build_bundle("9999", missing, tmp_path, fetch=_fake_edgar())


def test_load_manifest_roundtrip(tmp_path):
    m1 = build_bundle("9999", SPECS, tmp_path, fetch=_fake_edgar())
    m2 = load_manifest(tmp_path)
    assert isinstance(m2, Manifest)
    assert m2.to_dict() == m1.to_dict()


def test_docspec_validates_accession():
    with pytest.raises(ValueError):
        DocSpec(doc_id="x", accession="bogus")


def test_fetcher_rejects_non_edgar_url():
    with pytest.raises(EdgarError, match="non-EDGAR"):
        EdgarFetcher().get("https://example.com/data.htm")


def test_fetcher_throttle_sleeps(monkeypatch):
    f = EdgarFetcher(min_interval=0.3)
    slept = []
    monkeypatch.setattr("time.sleep", lambda s: slept.append(s))
    f._last_request = 0.0
    monkeypatch.setattr("time.monotonic", lambda: 0.0)
    f.throttle()  # first call, no sleep needed
    monkeypatch.setattr("time.monotonic", lambda: 0.1)
    f.throttle()  # 0.1s elapsed -> sleep 0.2s
    assert slept and abs(slept[-1] - 0.2) < 1e-9
