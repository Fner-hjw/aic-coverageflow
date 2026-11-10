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

import copy
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
        if self.path.exists():
            # A corrupt registry must NOT be silently replaced by the baseline:
            # the next save() would overwrite the audit trail of every version
            # ever registered. Fail loudly and let a human restore the file.
            try:
                data = statestore.read_json(self.path)
            except (OSError, ValueError) as exc:
                raise ValueError(
                    f"version registry at {self.path} is unreadable ({exc}); "
                    "refusing to reset it to the baseline - restore the file "
                    "or move it aside deliberately"
                ) from exc
            if not isinstance(data, dict):
                raise ValueError(
                    f"version registry at {self.path} is not a JSON object; "
                    "refusing to reset it to the baseline"
                )
            if not isinstance(data.get("versions"), list) or not data["versions"]:
                raise ValueError(
                    f"version registry at {self.path} has no version entries; "
                    "refusing to reset it to the baseline"
                )
            if not isinstance(data.get("active"), str) or not data["active"]:
                raise ValueError(
                    f"version registry at {self.path} has no active pointer; "
                    "refusing to reset it to the baseline"
                )
        else:
            data = {"active": BASELINE_VERSION,
                    "versions": [copy.deepcopy(_BASELINE_ENTRY)]}
        self._data = data

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
        if not isinstance(version, str) or not version.strip():
            raise ValueError(
                f"version must be a non-empty string, got {version!r}"
            )
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
        if decision != "APPROVED" and version == self.active():
            # rejecting the active version would leave the pointer aimed at a
            # rejected config; rollback is the supported way to move off it
            raise RuntimeError(
                f"cannot {decision.lower()} {version!r}: it is the active "
                "version; use rollback to move the pointer first"
            )
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
        rolled = self.get(active)
        rolled["rollback_to"] = to_version
        # the audit trail lives in the file, not only in this return value
        rolled["rollback"] = {"to": to_version, "by": by, "reason": reason,
                              "ts": statestore.utc_now_iso()}
        self._data["active"] = to_version
        self.save()
        return {"active": to_version, "rolled_back_from": active,
                "by": by, "reason": reason}

    # ------------------------------------------------------- config bundles
    def _inherited(self, version: str, key: str) -> Any:
        """Merge ``tested[key]`` along the parent chain, child winning per key.

        A version that only changes one prompt card must inherit the rest from
        its parent, not silently fall back to the baseline bundle.
        """
        seen: set[str] = set()
        chain: list[dict] = []
        cur: str | None = version
        while cur is not None and cur not in seen:
            seen.add(cur)
            entry = self.get(cur)
            chain.append(entry)
            cur = entry.get("parent")
        merged: Any = None
        for entry in reversed(chain):  # root first, child last
            value = (entry.get("tested") or {}).get(key)
            if value is None:
                continue
            if isinstance(value, dict):
                merged = {**(merged if isinstance(merged, dict) else {}), **value}
            else:
                merged = value
        return merged

    def config_versions(self, version: str | None = None) -> dict:
        """The pinned config bundle (prompt cards, mappings, engines) a NEW
        case should be created with, taken from the active (or given) entry."""
        version = version or self.active()
        self.get(version)  # raises LookupError for an unknown version
        # deep copy: prompt_cards holds a nested list ("critics"), and a
        # shallow dict() would hand callers a live alias into the registry
        return {
            "prompt_cards": copy.deepcopy(
                self._inherited(version, "prompt_cards")
                or _BASELINE_ENTRY["tested"]["prompt_cards"]),
            "mapping_tables": self._inherited(version, "model") or "1.0",
            "engines": self._inherited(version, "engines") or "1.0",
            "schema": "1.0",
        }

    def pinned_config_for_case(self, case_dir: Path | str) -> dict | None:
        """Read an existing case's ORIGINAL config versions (never rewritten)."""
        state = statestore.load_state(Path(case_dir))
        if state is None:
            return None
        return state.get("config_versions")
