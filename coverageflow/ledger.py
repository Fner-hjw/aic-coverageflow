"""Append-only findings ledger (owner: stream A2).

Per contracts/state-and-artifacts.md section 8, findings.json holds one row
per finding with machine-readable fields. Ledger rules (design-decisions §3):
  - Append-only: a finding closed and later retriggered becomes a NEW row.
  - A row closes ONLY by critic-verified evidence: artifact path + sha256.
  - Open findings are carried into every next request / repair packet.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from . import statestore

LEDGER_FILE = "findings.json"

_OK = {"artifact", "sha256"}

# Critics invent their own dedup keys, and they put the round in them:
# `c4|round-008-artifacts|version-pinning-unverifiable` and
# `c4|round-014-artifacts|version-pinning-unverifiable` are the same complaint.
# Left alone, every recurrence opens a new row and the ledger only grows, so
# the candidate can never converge. The round is not part of a finding's
# identity; the complaint is.
_ROUND_IN_KEY = re.compile(
    r"\b(?:round[-_ ]?|r)(\d{1,4})\b"      # round-014, round_14, r14
    r"|\b(\d{3})-artifacts\b"              # 014-artifacts
    r"|\bround\s+\d{1,4}\b",
    re.IGNORECASE,
)


def normalize_dedup_key(key: str) -> str:
    """Strip the round from a dedup key so a recurrence maps to one row."""
    if not isinstance(key, str) or not key:
        return key
    out = _ROUND_IN_KEY.sub("round-N", key)
    return re.sub(r"\s+", " ", out).strip().lower()


_CHECK_ID_FIELDS = ("check_id", "rubric_check_id", "rubric_check", "check")


def compute_dedup_key(finding: dict) -> str:
    """Identity of a finding, computed in code.

    Formula: ``normalize(source_critic | check_id | sorted(affected_artifacts))``
    where ``check_id`` is the rubric check id the finding cites. Only when no
    check id is present does the model-supplied ``dedup_key`` (or, last, the
    critic's own finding_id) serve as a *hint* in the check slot: it is
    normalised (round stripped) but otherwise trusted no further, because
    models reword it every round. The hint is never used when a check id
    exists.
    """
    check = ""
    for k in _CHECK_ID_FIELDS:
        v = finding.get(k)
        if isinstance(v, str) and v.strip():
            check = v.strip()
            break
    if not check:
        check = str(finding.get("dedup_key") or finding.get("finding_id") or "")
    arts = sorted(str(a) for a in (finding.get("affected_artifacts") or []))
    raw = "|".join([str(finding.get("source_critic") or ""), check,
                    ",".join(arts)])
    return normalize_dedup_key(raw)


# Severities that stop acceptance. A critic that marks a finding ``minor`` is
# saying the note can ship with it outstanding; the residual is carried into
# the deliverable as an advisory note instead of blocking the case forever.
BLOCKING_SEVERITIES = frozenset({"major", "critical"})


# ---------------------------------------------------------------------------
# Finding owners (diagnosis 3.2). An owner says which party can repair the
# defect. Only writer/model/extractor findings are repair work; system and
# mandate findings stop the case (needs_system_change / needs_mandate_change).
# ---------------------------------------------------------------------------
OWNERS = ("writer", "model", "extractor", "system", "mandate")
REPAIRABLE_OWNERS = frozenset({"writer", "model", "extractor"})
STOP_OWNERS = frozenset({"system", "mandate"})

GATE_OWNERS = {
    "facts_schema": "extractor", "citation_presence": "extractor",
    "period_coverage": "extractor",
    "forecast_bounds": "model", "terminal_reconciliation": "model",
    "report_evidence_links": "writer",
    "input_cutoff": "system", "candidate_manifest_integrity": "system",
    "controls_conformance": "system", "recompute": "system",
}

# Affected artifact -> owner, in pipeline order: the earliest affected
# artifact decides (a defect in facts.json is upstream of everything built on
# it). Engine outputs and manifests are harness output, not role output.
ARTIFACT_OWNERS = (
    ("ingest-report.json", "extractor"),
    ("facts.json", "extractor"),
    ("forecast_assumptions.json", "model"),
    ("model.json", "system"),
    ("valuation.json", "system"),
    ("report.md", "writer"),
    ("candidate-manifest.json", "system"),
    ("merged-findings.json", "system"),
)


def _artifact_rank(art: str) -> tuple[int, str] | None:
    base = str(art).replace("\\", "/").rsplit("/", 1)[-1]
    for i, (name, owner) in enumerate(ARTIFACT_OWNERS):
        if base == name:
            return i, owner
    return None


def assign_owner(finding: dict) -> str:
    """Owner of a finding, decided in code when it is opened.

    An explicit valid ``owner`` on the finding wins (gate findings from
    controls_conformance carry ``system``; a critic may set ``system`` or
    ``mandate``). Otherwise gate findings follow GATE_OWNERS and critic
    findings the earliest affected artifact. A finding that names no known
    artifact defaults to ``writer`` (the note is what a repair can change).
    """
    explicit = finding.get("owner")
    if isinstance(explicit, str) and explicit in OWNERS:
        return explicit
    src = str(finding.get("source_critic") or "")
    gate = src.split(":", 1)[1] if src.startswith("gate:") else finding.get("gate")
    if gate:
        return GATE_OWNERS.get(str(gate), "system")
    ranked = [r for r in (_artifact_rank(a)
                          for a in finding.get("affected_artifacts") or []) if r]
    if ranked:
        return min(ranked)[1]
    return "writer"


class FindingsLedger:
    """The per-case findings ledger (findings.json)."""

    def __init__(self, case_dir: Path | str):
        self.case_dir = Path(case_dir)
        self.path = self.case_dir / LEDGER_FILE
        self._rows: list[dict[str, Any]] = []
        self._load()

    # ------------------------------------------------------------------ io
    def _load(self) -> None:
        data = statestore.read_json_or_none(self.path)
        rows = [dict(r) for r in data["findings"]] \
            if data and "findings" in data else []
        # Backward-compatible read: rows written before ledger-assigned IDs
        # carry the critic's own (possibly colliding) finding_id and no
        # critic_ref. Migrate in memory: keep the old label as critic_ref and
        # give every row a unique ledger ID (persisted on the next save).
        if any("critic_ref" not in r for r in rows):
            for i, r in enumerate(rows, 1):
                r.setdefault("critic_ref", r.get("finding_id"))
                r["finding_id"] = self._fmt_id(i)
        for r in rows:      # rows written before owners existed
            if r.get("owner") not in OWNERS:
                r["owner"] = assign_owner(r)
        self._rows = rows

    @staticmethod
    def _fmt_id(n: int) -> str:
        return f"fnd-{n:04d}"

    def _next_id(self) -> str:
        # monotonic over ALL rows (open and closed); rows are never removed
        return self._fmt_id(len(self._rows) + 1)

    def save(self) -> str:
        return statestore.atomic_write_json(self.path, {
            "case_id": (statestore.load_state(self.case_dir) or {}).get("case_id"),
            "findings": self._rows,
        })

    # -------------------------------------------------------------- queries
    def all_findings(self) -> list[dict]:
        return [dict(r) for r in self._rows]

    def by_id(self, finding_id: str) -> dict:
        for r in self._rows:
            if r["finding_id"] == finding_id:
                return dict(r)
        raise LookupError(finding_id)

    def open_findings(self) -> list[dict]:
        return [r for r in self._rows if r["status"] == "open"]

    def blocking_findings(self) -> list[dict]:
        """Open findings a critic marked as blocking (critic's own severity;
        ``minor`` means the note can ship with it outstanding)."""
        return [r for r in self.open_findings()
                if r.get("severity") in BLOCKING_SEVERITIES]

    def repairable_findings(self) -> list[dict]:
        """Open blocking findings a role can repair (owner writer/model/extractor)."""
        return [r for r in self.blocking_findings()
                if r.get("owner") in REPAIRABLE_OWNERS]

    def stopping_findings(self) -> list[dict]:
        """Open blocking findings only the harness or a human can change."""
        return [r for r in self.blocking_findings()
                if r.get("owner") in STOP_OWNERS]

    def carried(self) -> list[dict]:
        """Open findings as carried payload (used in requests/repair packets).
        ``finding_id`` is the LEDGER id critics must cite in resolutions."""
        return [dict(r) for r in self._rows if r["status"] == "open"]

    # -------------------------------------------------------------- writes
    def open_finding(self, finding: dict, round: int) -> str:
        """Append a finding, or bump last_seen_round on an open row with the
        same computed dedup_key. Returns the LEDGER id. Append-only: a key
        matching only a CLOSED row starts a new row. The critic-supplied
        finding_id is stored as ``critic_ref``."""
        key = compute_dedup_key(finding)
        for r in self._rows:
            if r["status"] == "open" and r.get("dedup_key") == key:
                r["last_seen_round"] = round
                if str(r.get("source_critic") or "").startswith("engine:"):
                    # the engine names period and figures; the repair must see
                    # the CURRENT violation, not the first round's numbers
                    r["summary"] = finding.get("summary", r.get("summary", ""))
                    r["required_evidence"] = finding.get(
                        "required_evidence", r.get("required_evidence", ""))
                self.save()
                return r["finding_id"]
        finding_id = self._next_id()
        row = {
            "finding_id": finding_id,
            "critic_ref": finding.get("finding_id"),
            "dedup_key": key,
            "source_critic": finding.get("source_critic"),
            "first_seen_round": round,
            "last_seen_round": round,
            "status": "open",
            "closed_by": None,
            "severity": finding.get("severity", "minor"),
            "summary": finding.get("summary", ""),
            "affected_artifacts": list(finding.get("affected_artifacts", [])),
            "required_evidence": finding.get("required_evidence", ""),
            "owner": assign_owner(finding),
        }
        self._rows.append(row)
        self.save()
        return finding_id

    def close_finding(self, finding_id: str, *, critic: str, round: int,
                      verified_evidence: dict) -> bool:
        """Close a finding ONLY with critic-verified evidence (artifact+sha256).

        Raises ValueError on missing evidence, LookupError on an unknown
        ledger id, and ValueError if the row is already closed. Returns True
        after a real state change (it never returns False; the boolean lets
        callers assert that an event may be recorded).
        """
        if not verified_evidence or not all(verified_evidence.get(k) for k in _OK):
            raise ValueError("closing requires verified_evidence {artifact, sha256}")
        for r in self._rows:
            if r["finding_id"] == finding_id:
                if r["status"] == "closed":
                    raise ValueError(f"finding already closed: {finding_id}")
                r["status"] = "closed"
                r["closed_by"] = {
                    "round": round,
                    "critic": critic,
                    "verified_evidence": {
                        "artifact": verified_evidence["artifact"],
                        "sha256": verified_evidence["sha256"],
                    },
                }
                self.save()
                return True
        raise LookupError(f"unknown finding: {finding_id}")
