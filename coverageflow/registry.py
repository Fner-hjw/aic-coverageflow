"""Version registry (owner: stream A2) per contracts section 9.

versions/registry.json holds the version entries and the ACTIVE pointer.
Locked evolution rules (design-decisions section 5):
  - only human-APPROVED versions can become active (pointer swap);
  - rejected/deferred proposals retain the current active version;
  - rollback restores the prior pointer and RECORDS rollback_to on the entry;
    entries are kept for audit;
  - existing cases stay pinned to their ORIGINAL version: their state's
    config_versions was written at case creation and is never rewritten by
    the registry; only NEW cases take the active config.

The registry never touches case directories except read-only
(pinned_config_for_case reads the case state).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import statestore

BASELINE_VERSION = "sys-1.0"

_BASELINE_ENTRY: dict[str, Any] = {
    "version": BASELINE_VERSION,
    "parent": None,
    "scope": "baseline",
    "proposal": {"author": "kickoff", "diff_files": [], "diff_sha256": None},
    "tested": {"model": "1.0",
               "prompt_cards": {"ingest": "1.0", "extractor": "1.0",
                                "model": "1.0", "writer": "1.0",
                                "critics": ["1.0"]},
               "engines": "1.0", "cases": []},
    "review": {"fresh_critic_findings": [], "regressions_all_pass": True,
               "identical_outputs_proof": {}},
    "approval": {"decision": "APPROVED", "by": "kickoff", "ts": None},
    "rollback_to": None,
}

_REQUIRED_ENTRY_KEYS = ("version", "parent", "scope", "proposal", "tested")


class VersionRegistry:
    """JSON-file registry with atomic writes (via statestore)."""

    def __init__(self, path: Path | str):
        self.path = Path(path)
        data = statestore.read_json_or_none(self.path) if self.path.exists() else None
        if not data:
            data = {"active": BASELINE_VERSION,
                    "versions": [dict(_BASELINE_ENTRY)]}
        self._data = data
        if not self._data.get("versions"):
            raise ValueError("registry has no version entries")

    # ---------------------------------------------------------------- io
    def save(self) -> None:
        statestore.atomic_write_json(self.path, self._data)

    def reload(self) -> None:
        self._data = statestore.read_json(self.path)

    # ------------------------------------------------------------- queries
    def active(self) -> str:
        return self._data["active"]

    def versions(self) -> list[dict]:
        return [dict(v) for v in self._data["versions"]]

    def get(self, version: str) -> dict:
        for v in self._data["versions"]:
            if v["version"] == version:
                return v
        raise LookupError(version)

    def _has(self, version: str) -> bool:
        try:
            self.get(version)
            return True
        except LookupError:
            return False

    # ------------------------------------------------------------- writes
    def register(self, entry: dict) -> dict:
        """Append a new version entry (unapproved). Validates parents & keys."""
        for key in _REQUIRED_ENTRY_KEYS:
            if key not in entry:
                raise ValueError(f"version entry missing required key {key!r}")
        version = entry["version"]
        if self._has(version):
            raise ValueError(f"version {version!r} already registered")
        parent = entry["parent"]
        if parent is not None and not self._has(parent):
            raise ValueError(f"unknown parent version {parent!r}")
        record = {
            "version": version,
            "parent": parent,
            "scope": entry["scope"],
            "proposal": dict(entry["proposal"]),
            "tested": dict(entry["tested"]),
            "review": None,
            "approval": None,
            "rollback_to": None,
        }
        self._data["versions"].append(record)
        self.save()
        return dict(record)

    def record_review(self, version: str, *, fresh_critic_findings: list,
                      regressions_all_pass: bool, identical_outputs_proof: dict
                      ) -> dict:
        self.get(version)   # raises LookupError if unknown
        entry = self.get(version)
        entry["review"] = {
            "fresh_critic_findings": list(fresh_critic_findings),
            "regressions_all_pass": bool(regressions_all_pass),
            "identical_outputs_proof": dict(identical_outputs_proof),
        }
        self.save()
        return entry

    def approve(self, version: str, *, by: str, decision: str) -> dict:
        """Record the human decision; APPROVED swaps the active pointer."""
        if decision not in ("APPROVED", "REJECTED", "DEFERRED"):
            raise ValueError("decision must be APPROVED|REJECTED|DEFERRED")
        entry = self.get(version)
        review = entry.get("review") or {}
        if decision == "APPROVED" and not review.get("regressions_all_pass"):
            raise RuntimeError("cannot approve a version whose fresh-critic "
                               "review did not pass all regressions")
        if entry.get("rollback_to"):
            raise RuntimeError("cannot approve a rolled-back version")
        entry["approval"] = {"decision": decision, "by": by,
                             "ts": statestore.utc_now_iso()}
        if decision == "APPROVED":
            self._data["active"] = version
        self.save()
        return entry

    def rollback(self, to_version: str, *, by: str, reason: str = "") -> dict:
        """Restore a prior pointer; the currently-active entry records
        rollback_to; existing cases keep their original versions."""
        active = self.active()
        if not self._has(to_version):
            raise LookupError(to_version)
        if to_version == active:
            raise RuntimeError(f"nothing to roll back: {to_version!r} is active")
        approved_prior = self.get(to_version)
        if (approved_prior.get("approval") or {}).get("decision") != "APPROVED":
            raise RuntimeError(f"cannot roll back to unapproved {to_version!r}")
        if approved_prior.get("rollback_to") is not None:
            raise RuntimeError(f"cannot roll back to a rolled-back version")
        self.get(active)["rollback_to"] = to_version
        self._data["active"] = to_version
        self.save()
        return {"active": to_version, "rolled_back_from": active,
                "by": by, "reason": reason}

    # ------------------------------------------------------- config bundles
    def config_versions(self, version: str | None = None) -> dict:
        """The pinned config bundle (prompt cards, mappings, engines) a NEW
        case should be created with, taken from the active (or given) entry."""
        entry = self.get(version or self.active())
        tested = entry.get("tested") or {}
        return {
            "prompt_cards": dict(tested.get("prompt_cards")
                                 or _BASELINE_ENTRY["tested"]["prompt_cards"]),
            "mapping_tables": tested.get("model") or "1.0",
            "engines": tested.get("engines") or "1.0",
            "schema": "1.0",
        }

    def pinned_config_for_case(self, case_dir: Path | str) -> dict | None:
        """Read an existing case's ORIGINAL config versions (never rewritten)."""
        state = statestore.load_state(Path(case_dir))
        if state is None:
            return None
        return state.get("config_versions")
