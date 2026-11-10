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
        self._rows = list(data["findings"]) if data and "findings" in data else []

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

    def carried(self) -> list[dict]:
        """Open findings as carried payload (used in requests/repair packets)."""
        return [dict(r) for r in self._rows if r["status"] == "open"]

    # -------------------------------------------------------------- writes
    def open_finding(self, finding: dict, round: int) -> str:
        """Append a finding (or bump last_seen_round on an identical open row).

        Returns the finding_id. Append-only: a dedup_key matching a CLOSED row
        starts a new row.
        """
        key = normalize_dedup_key(finding.get("dedup_key")
                                  or finding.get("finding_id"))
        for r in self._rows:
            if r["status"] == "open" and r.get("dedup_key") == key:
                r["last_seen_round"] = round
                self.save()
                return r["finding_id"]
        finding_id = finding.get("finding_id") or f"fnd-{len(self._rows) + 1:03d}"
        row = {
            "finding_id": finding_id,
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
        }
        self._rows.append(row)
        self.save()
        return finding_id

    def close_finding(self, finding_id: str, *, critic: str, round: int,
                      verified_evidence: dict) -> None:
        """Close a finding ONLY with critic-verified evidence (artifact+sha256)."""
        if not verified_evidence or not all(verified_evidence.get(k) for k in _OK):
            raise ValueError("closing requires verified_evidence {artifact, sha256}")
        for r in self._rows:
            if r["finding_id"] == finding_id:
                if r["status"] == "closed":
                    return
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
                return
        raise LookupError(f"unknown finding: {finding_id}")
