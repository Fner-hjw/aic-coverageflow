"""Verified resume for interrupted cases (owner: stream A2).

Locked rules (docs/design-decisions.md §3):
  - On run start: inspect state + artifact hashes; DISCARD unverified partial
    outputs (moved to rounds/round-NNN/discarded/ for a forensic trail);
  - re-dispatch ONLY the pending role;
  - completed verified stages return their saved artifacts (no re-dispatch);
  - a completed case returns its saved result untouched.

Verified artifacts come from the checkpoint history sidecars written by
statestore (history/checkpoint-*.json); each checkpoint carries a cumulative
path -> sha256 map. A crash between artifact write and checkpoint write is
handled conservatively: the artifact is NOT in the verified map, so it is
discarded and the producing role re-dispatches.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import statestore

DISCARD_DIRNAME = "discarded"

# stage order used to express "roll back to the earliest broken stage"
STAGE_INDEX = {
    "ingest": 0, "extractor": 1, "model": 2, "writer": 3,
    "engine": 4, "candidate": 5, "gates": 6, "critics": 7, "merge": 8,
}

ARTIFACT_STAGE = {
    "ingest-report.json": "ingest",
    "facts.json": "extractor",
    "forecast_assumptions.json": "model",
    "report.md": "writer",
    # the deterministic engines run as a side effect of the model stage, so a
    # broken engine artifact must rewind to model -- mapping it to the later
    # "engine" index rewound past its producer and left the writer with no
    # model.json or valuation.json to cite
    "model.json": "model",
    "valuation.json": "model",
    "candidate-manifest.json": "candidate",
    "merged-findings.json": "merge",
}


def artifact_stage(rel_path: str) -> str:
    """Stage owning a (round-relative) artifact path."""
    name = Path(rel_path).name
    if name in ARTIFACT_STAGE:
        return ARTIFACT_STAGE[name]
    import re
    if re.fullmatch(r"critic_c[1-4]\.json", name):
        return "critics"
    return "unknown"


@dataclass
class ResumePlan:
    verified: dict[str, str]          # rel path -> sha256 for intact artifacts
    discarded: list[str] = field(default_factory=list)  # rel paths moved to discarded/
    broken_verified: list[str] = field(default_factory=list)  # verified but no longer matching
    earliest_stage: str | None = None  # stage to resume from (may roll back)
    notes: list[str] = field(default_factory=list)


def load_verified_map(case_dir: Path) -> dict[str, str]:
    """Cumulative verified artifact map from the latest checkpoint sidecar."""
    hist_dir = Path(case_dir) / statestore.HISTORY_DIR
    best: dict[str, str] = {}
    if not hist_dir.exists():
        return best
    for ck_file in sorted(hist_dir.glob("checkpoint-*.json")):
        rec = statestore.read_json_or_none(ck_file)
        if rec and isinstance(rec.get("verified_artifacts"), dict):
            best = rec["verified_artifacts"]  # later checkpoints are cumulative
    return best


def plan_resume(state: dict, case_dir: Path,
                 hash_file: Callable[[Path], str] = statestore.sha256_file
                 ) -> ResumePlan:
    """Compute what survives and what gets discarded; return the resume plan.

    The plan is pure: it does not move files. The orchestrator applies it via
    :func:`apply_plan`.
    """
    case_dir = Path(case_dir)
    verified = load_verified_map(case_dir)
    intact: dict[str, str] = {}
    broken: list[str] = []
    for rel, sha in verified.items():
        path = case_dir / rel
        if path.exists() and hash_file(path) == sha:
            intact[rel] = sha
        else:
            broken.append(rel)

    pending = state.get("pending") or {}
    round_no = int(pending.get("round") or 0)
    stage = pending.get("stage")
    if stage is None:
        stage = "ingest" if pending.get("role") == "generator" else "merge"

    # unverified partials in the CURRENT round must be discarded
    discard_candidates: list[str] = []
    if round_no > 0:
        round_dir = case_dir / "rounds" / f"round-{round_no:03d}"
        if round_dir.exists():
            for path in sorted(round_dir.rglob("*")):
                if not path.is_file() or path.parent.name == DISCARD_DIRNAME:
                    continue
                rel = str(path.relative_to(case_dir))
                if rel not in intact:
                    discard_candidates.append(rel)

    # roll back to the earliest broken verified stage, but no later than the
    # recorded pending stage
    stages = [STAGE_INDEX.get(stage, STAGE_INDEX["ingest"])]
    for rel in broken:
        s = artifact_stage(rel)
        if s in STAGE_INDEX:
            stages.append(STAGE_INDEX[s])
    # A stage can also be missing without being "broken": an artifact written
    # by a process that died before its checkpoint never entered the verified
    # map, so nothing above notices it. Resuming past it hands the next role an
    # empty input and it fails every retry. Anything absent from `intact` is
    # about to be discarded, so its stage must re-run.
    if round_no > 0:
        for name, owner in ARTIFACT_STAGE.items():
            if owner not in STAGE_INDEX:
                continue
            rel = f"rounds/round-{round_no:03d}/generator/{name}"
            if rel not in intact:
                stages.append(STAGE_INDEX[owner])
    earliest = min(stages)
    earliest_stage = next(k for k, v in STAGE_INDEX.items() if v == earliest)

    # dropping back to an earlier stage invalidates the current round's
    # verified artifacts for ALL later stages of that round: they derive from
    # content that is about to change, so they must re-run
    stale_downstream: list[str] = []
    round_prefix = f"rounds/round-{round_no:03d}/"
    for rel in list(intact.keys()):
        if not rel.startswith(round_prefix):
            continue
        s = artifact_stage(rel)
        if s in STAGE_INDEX and STAGE_INDEX[s] >= earliest:
            stale_downstream.append(rel)
            intact.pop(rel)

    notes = []
    for rel in broken:
        notes.append(f"verified artifact broken: {rel}")
    for rel in discard_candidates + stale_downstream:
        notes.append(f"unverified partial output discarded: {rel}")

    return ResumePlan(
        verified=intact,
        discarded=discard_candidates + stale_downstream,
        broken_verified=broken,
        earliest_stage=earliest_stage,
        notes=notes,
    )


def apply_plan(plan: ResumePlan, case_dir: Path) -> None:
    """Move discarded partials to a discarded/ dir beside them for forensics."""
    case_dir = Path(case_dir)
    for rel in plan.discarded:
        path = case_dir / rel
        if not path.exists():
            continue
        discard_dir = path.parent / DISCARD_DIRNAME
        discard_dir.mkdir(parents=True, exist_ok=True)
        # keep every discard (a role can be re-killed more than once)
        n = sum(1 for _ in discard_dir.glob(path.name + "*"))
        path.rename(discard_dir / f"{path.name}.{n:02d}.partial")
