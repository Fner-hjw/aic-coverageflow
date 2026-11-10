"""EDGAR bundle builder: fetch, snapshot, and hash SEC filings into a frozen bundle.

Owner stream: A1 (runtime & data).

Contract (docs/contracts/interfaces.md section 4)::

    def build_bundle(cik: str, doc_specs: list[DocSpec], dest: Path) -> Manifest

A bundle is a directory containing ``inputs/`` (the raw primary documents,
byte-exact EDGAR snapshots) and ``manifest.json`` per
docs/contracts/state-and-artifacts.md section 4:

    {"as_of_cutoff": ..., "documents": [{doc_id, form, filed, period, path,
     sha256, bytes, source_url}, ...], "bundle_id": ..., "bundle_sha256": ...}

Politeness: every request to SEC EDGAR sends a User-Agent identifying this
research project and sleeps at least MIN_INTERVAL seconds after the previous
request (SEC fair-access guidance). The network is only used when building
bundles during development; the test suite runs fully offline via an
injectable ``fetch`` callable.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

USER_AGENT = "CoverageFlow research project coverageflow@example.com"
MIN_INTERVAL_SECONDS = 0.3
SEC_HOSTS = ("www.sec.gov", "data.sec.gov")
_ACCESSION_RE = re.compile(r"^\d{10}-\d{2}-\d{6}$")

Fetch = Callable[[str], bytes]


class EdgarError(Exception):
    """Base class for bundle-building errors."""


class FetchError(EdgarError):
    """Raised when an EDGAR request could not be completed."""


class SpecMismatchError(EdgarError):
    """Raised when a DocSpec disagrees with what EDGAR reports for the accession."""


@dataclass(frozen=True)
class DocSpec:
    """Request for one document inside a bundle.

    ``accession`` is the dashed SEC accession number (e.g.
    ``0000050863-25-000009``). ``form``/``filed``/``period`` are optional
    verification data: when provided they are checked against the EDGAR
    submissions record and a mismatch raises SpecMismatchError. When
    ``primary_doc`` is omitted it is resolved from the submissions API.
    ``ticker`` (optional, e.g. "INTC") is used for the bundle_id.
    """

    doc_id: str
    accession: str
    form: str | None = None
    filed: str | None = None
    period: str | None = None
    primary_doc: str | None = None
    ticker: str | None = None

    def __post_init__(self) -> None:
        if not _ACCESSION_RE.match(self.accession):
            raise ValueError(
                f"accession must look like 0000050863-25-000009, got {self.accession!r}"
            )
        if not self.doc_id or not re.match(r"^[a-z0-9][a-z0-9.-]*$", self.doc_id):
            raise ValueError(f"doc_id must be a lowercase slug, got {self.doc_id!r}")


@dataclass(frozen=True)
class ManifestDocument:
    doc_id: str
    form: str
    filed: str
    period: str
    path: str
    sha256: str
    bytes: int
    source_url: str

    def to_dict(self) -> dict:
        return {
            "doc_id": self.doc_id,
            "form": self.form,
            "filed": self.filed,
            "period": self.period,
            "path": self.path,
            "sha256": self.sha256,
            "bytes": self.bytes,
            "source_url": self.source_url,
        }


@dataclass(frozen=True)
class Manifest:
    as_of_cutoff: str
    bundle_id: str
    bundle_sha256: str
    documents: list[ManifestDocument] = field(default_factory=list)

    @property
    def doc_ids(self) -> list[str]:
        return [d.doc_id for d in self.documents]

    def to_dict(self) -> dict:
        return {
            "as_of_cutoff": self.as_of_cutoff,
            "documents": [d.to_dict() for d in self.documents],
            "bundle_id": self.bundle_id,
            "bundle_sha256": self.bundle_sha256,
        }

    def write_json(self, dest: Path) -> Path:
        """Atomically write manifest.json to dest. Returns its path."""
        return _atomic_write_json(dest / "manifest.json", self.to_dict())

    def verify(self, dest: Path) -> None:
        """Re-hash every document in dest and confirm the manifest still matches."""
        for d in self.documents:
            p = dest / d.path
            if not p.is_file():
                raise EdgarError(f"missing bundle file {d.path}")
            blob = p.read_bytes()
            if len(blob) != d.bytes or hashlib.sha256(blob).hexdigest() != d.sha256:
                raise EdgarError(f"hash/size mismatch for {d.path}")
        if _bundle_sha(self.documents) != self.bundle_sha256:
            raise EdgarError("bundle_sha256 mismatch")


class EdgarFetcher:
    """Rate-limited EDGAR GET client returning raw bytes."""

    def __init__(self, min_interval: float = MIN_INTERVAL_SECONDS, user_agent: str = USER_AGENT):
        self.min_interval = min_interval
        self.user_agent = user_agent
        self._last_request = 0.0
        self.urls: list[str] = []

    def get(self, url: str, attempts: int = 4) -> bytes:
        parsed = url.split("/", 3)
        if len(parsed) < 3 or parsed[2] not in SEC_HOSTS:
            raise FetchError(f"refusing non-EDGAR url: {url}")
        import urllib.request

        last_exc: Exception | None = None
        for attempt in range(attempts):
            self.throttle()
            req = urllib.request.Request(url, headers={"User-Agent": self.user_agent})
            try:
                with urllib.request.urlopen(req, timeout=90) as resp:
                    data = resp.read()
                self.urls.append(url)
                return data
            except Exception as exc:  # urllib raises several exception types
                last_exc = exc
                if attempt < attempts - 1:
                    time.sleep(2.0 * (attempt + 1))
        raise FetchError(f"GET {url} failed after {attempts} attempts: {last_exc}") from last_exc

    def throttle(self) -> None:
        wait = self.min_interval - (time.monotonic() - self._last_request)
        if wait > 0:
            time.sleep(wait)
        self._last_request = time.monotonic()


def submissions_url(cik: str) -> str:
    return f"https://data.sec.gov/submissions/CIK{int(cik):010d}.json"


def archive_base(cik: str, accession: str) -> str:
    return f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession.replace('-', '')}"


def _bundle_sha(documents: list[ManifestDocument]) -> str:
    """Bundle hash: sha256 over the ordered per-document sha256 strings."""
    h = hashlib.sha256()
    for d in documents:
        h.update(d.sha256.encode("utf-8"))
    return h.hexdigest()


def fetch_submissions(cik: str, fetch: Fetch) -> dict:
    blob = fetch(submissions_url(cik))
    try:
        return json.loads(blob.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FetchError(f"submissions JSON for CIK {cik} unparsable: {exc}") from exc


def _recent_index(submissions: dict) -> dict[str, dict]:
    """Map accession number -> submissions record (recent filings window).

    The live EDGAR API nests the recent window under ``filings.recent``; the
    flat top-level shape is also accepted (synthetic fixtures).
    """
    recent = submissions.get("filings", {}).get("recent") or submissions.get("recent", {})
    keys = recent.get("accessionNumber", [])
    idx: dict[str, dict] = {}
    for i, acc in enumerate(keys):
        idx[acc.replace("-", "")] = {
            "accessionNumber": acc,
            "form": recent.get("form", [""] * len(keys))[i],
            "filingDate": recent.get("filingDate", [""] * len(keys))[i],
            "reportDate": recent.get("reportDate", [""] * len(keys))[i],
            "primaryDocument": recent.get("primaryDocument", [""] * len(keys))[i],
        }
    return idx


def _verify(value: str | None, cik_value: str, what: str, doc_id: str) -> str:
    if value is not None and value != cik_value:
        raise SpecMismatchError(
            f"DocSpec for {doc_id}: {what}={value!r} but EDGAR reports {cik_value!r}"
        )
    return cik_value


def build_bundle(
    cik: str,
    doc_specs: list[DocSpec],
    dest: Path,
    *,
    fetch: Fetch | None = None,
) -> Manifest:
    """Fetch each doc in doc_specs from EDGAR, snapshot into dest, return the Manifest.

    Documents are stored at ``dest/inputs/<doc_id>.<ext>`` and the manifest at
    ``dest/manifest.json`` (atomic writes). The manifest documents list is
    ordered by doc_id so the bundle hash is reproducible.
    ``fetch`` maps a URL to raw bytes; it defaults to a rate-limited EDGAR
    client and is injectable for offline tests.
    """
    if not doc_specs:
        raise ValueError("doc_specs is empty")
    fetch = fetch or EdgarFetcher().get
    submissions = fetch_submissions(cik, fetch)
    recent = _recent_index(submissions)

    inputs_dir = dest / "inputs"
    os.makedirs(inputs_dir, exist_ok=True)
    seen: set[str] = set()
    documents: list[ManifestDocument] = []
    tickers = {s.ticker for s in doc_specs if s.ticker}

    for spec in doc_specs:
        acc = spec.accession.replace("-", "")
        rec = recent.get(acc)
        if rec is None or not rec.get("primaryDocument"):
            raise SpecMismatchError(
                f"accession {spec.accession} for {spec.doc_id} not found in EDGAR submissions window"
            )
        form = _verify(spec.form, rec["form"], "form", spec.doc_id)
        filed = _verify(spec.filed, rec["filingDate"], "filed", spec.doc_id)
        period = _verify(spec.period, rec["reportDate"], "period", spec.doc_id)
        primary = spec.primary_doc or rec["primaryDocument"]
        if spec.doc_id in seen:
            raise ValueError(f"duplicate doc_id {spec.doc_id!r}")
        seen.add(spec.doc_id)

        url = f"{archive_base(cik, spec.accession)}/{primary}"
        blob = fetch(url)
        ext = Path(primary).suffix or ".htm"
        rel_path = f"inputs/{spec.doc_id}{ext}"
        _atomic_write_bytes(dest / rel_path, blob)

        documents.append(
            ManifestDocument(
                doc_id=spec.doc_id,
                form=form,
                filed=filed,
                period=period,
                path=rel_path,
                sha256=hashlib.sha256(blob).hexdigest(),
                bytes=len(blob),
                source_url=url,
            )
        )

    documents.sort(key=lambda d: d.doc_id)
    as_of = max(d.filed for d in documents)
    ticker = tickers.pop().lower() if len(tickers) == 1 else str(int(cik))
    manifest = Manifest(
        as_of_cutoff=as_of,
        bundle_id=f"{ticker}-{as_of[:4]}-{as_of[5:7]}",
        bundle_sha256=_bundle_sha(documents),
        documents=documents,
    )
    manifest.write_json(dest)
    return manifest


def find_bundle_dir(bundle_id: str, workspace: Path | str | None = None,
                    data_dirs: list[Path] | None = None) -> Path | None:
    """Locate a frozen bundle directory by its bundle_id.

    Search order: ``<workspace>/bundles/<bundle_id>``, then every
    ``<data_dir>/*/manifest.json`` whose manifest declares that bundle_id.
    Returns None when no bundle matches.
    """
    if not bundle_id:
        return None
    if workspace is not None:
        cand = Path(workspace) / "bundles" / bundle_id
        if (cand / "manifest.json").is_file():
            return cand
    for base in data_dirs or []:
        base = Path(base)
        if not base.is_dir():
            continue
        for man_path in sorted(base.glob("*/manifest.json")):
            try:
                data = json.loads(man_path.read_text("utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if data.get("bundle_id") == bundle_id:
                return man_path.parent
    return None


def materialize_case_inputs(case_dir: Path | str, manifest: dict,
                            bundle_dir: Path | str) -> list[str]:
    """Copy every document *manifest* declares from *bundle_dir* into the case.

    A case manifest carries bundle-relative paths (``inputs/<doc>.htm``); the
    case directory needs its own byte-identical copy before any role can read
    them. Each copy is verified against the manifest's ``sha256``/``bytes``
    when declared, so a case can never start on a corrupted snapshot.

    Returns the case-relative paths that were written. Raises EdgarError when
    the bundle is missing a declared document or a hash does not match.
    """
    case_dir = Path(case_dir)
    bundle_dir = Path(bundle_dir)
    written: list[str] = []
    for doc in (manifest or {}).get("documents") or []:
        rel = (doc or {}).get("path")
        if not rel:
            continue
        source = bundle_dir / rel
        if not source.is_file():
            raise EdgarError(
                f"bundle {bundle_dir} does not contain {rel} "
                f"(declared by doc {doc.get('doc_id')!r})")
        blob = source.read_bytes()
        want_sha = doc.get("sha256")
        if want_sha and hashlib.sha256(blob).hexdigest() != want_sha:
            raise EdgarError(f"sha256 mismatch for {rel} in bundle {bundle_dir}")
        want_bytes = doc.get("bytes")
        if want_bytes is not None and len(blob) != int(want_bytes):
            raise EdgarError(
                f"size mismatch for {rel}: {len(blob)} bytes, manifest says {want_bytes}")
        target = case_dir / rel
        if target.is_file() and target.read_bytes() == blob:
            written.append(str(rel))
            continue
        _atomic_write_bytes(target, blob)
        written.append(str(rel))
    return written


def load_manifest(bundle_dir: Path) -> Manifest:
    data = json.loads((bundle_dir / "manifest.json").read_text("utf-8"))
    documents = [ManifestDocument(**d) for d in data["documents"]]
    return Manifest(
        as_of_cutoff=data["as_of_cutoff"],
        bundle_id=data["bundle_id"],
        bundle_sha256=data["bundle_sha256"],
        documents=documents,
    )


def _atomic_write_bytes(path: Path, blob: bytes) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(str(path) + ".tmp")
    tmp.write_bytes(blob)
    os.replace(tmp, path)


def _atomic_write_json(path: Path, payload: dict) -> Path:
    blob = (json.dumps(payload, indent=2, sort_keys=False) + "\n").encode("utf-8")
    _atomic_write_bytes(path, blob)
    return path
