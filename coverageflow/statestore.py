"""State storage primitives for CoverageFlow (owner: stream A2).

This is the ONLY module that writes control state (state.json), checkpoints
(history/ + the state's checkpoints list) and the trace (trace.jsonl).
Every write is atomic: content is written to a temp file in the same
directory, flushed and fsynced, then moved into place with ``os.replace``.
All other streams that need state-file writes must go through these helpers
(docs/contracts/interfaces.md §7 coding rules).
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Callable

STATE_FILE = "state.json"
TRACE_FILE = "trace.jsonl"
HISTORY_DIR = "history"
RESULT_FILE = "result.json"
REQUEST_FILE = "request.json"

# ---------------------------------------------------------------------------
# time (injectable for determinism; callers may pass their own clock callable)
# ---------------------------------------------------------------------------

def utc_now_iso() -> str:
    """Current UTC time as ISO-8601 Z string."""
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# hashing helpers
# ---------------------------------------------------------------------------

def sha256_bytes(data: bytes) -> str:
    """SHA-256 hex digest of raw bytes."""
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path | str) -> str:
    """SHA-256 hex digest of a file's bytes (streamed)."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def canonical_json_bytes(obj: Any) -> bytes:
    """Deterministic JSON encoding (sorted keys, compact separators)."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def sha256_json(obj: Any) -> str:
    """SHA-256 of the canonical JSON encoding of *obj*."""
    return sha256_bytes(canonical_json_bytes(obj))


def sha256_dict_map(paths: list[Path | str], root: Path | None = None) -> dict[str, str]:
    """Map of relative path -> sha256 for each existing path."""
    root = Path(root) if root else None
    out: dict[str, str] = {}
    for p in paths:
        p = Path(p)
        if not p.exists():
            continue
        key = str(p.relative_to(root)) if root and str(root) in str(p) else str(p)
        out[key] = sha256_file(p)
    return out


# ---------------------------------------------------------------------------
# atomic writes / reads
# ---------------------------------------------------------------------------

def atomic_write_bytes(path: Path | str, data: bytes) -> str:
    """Atomically write bytes to *path*; returns the sha256 of the content."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix="." + path.name + ".")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return sha256_bytes(data)


def atomic_write_text(path: Path | str, text: str) -> str:
    """Atomically write UTF-8 text; returns the sha256 of the content."""
    return atomic_write_bytes(path, text.encode("utf-8"))


def atomic_write_json(path: Path | str, obj: Any) -> str:
    """Atomically write pretty JSON; returns the sha256 of the content."""
    data = json.dumps(obj, indent=2, sort_keys=False, ensure_ascii=False).encode("utf-8")
    return atomic_write_bytes(path, data)


def atomic_append_jsonl(path: Path | str, obj: Any) -> None:
    """Append one JSON object as a line to a jsonl file (durability over atomicity).

    JSONL appends cannot be a single os.replace; we flush+fsync the append so
    a crash leaves either the whole line or none of it (line < PIPE_BUF writes
    are effectively atomic on local filesystems).
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(obj, separators=(",", ":"), ensure_ascii=False) + "\n"
    with open(path, "a", encoding="utf-8") as f:
        f.write(line)
        f.flush()
        os.fsync(f.fileno())


def read_json(path: Path | str) -> Any:
    """Read a JSON file (raises FileNotFoundError if missing)."""
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def read_json_or_none(path: Path | str) -> Any:
    """Read a JSON file; returns None when missing or unparsable."""
    try:
        return read_json(path)
    except (OSError, json.JSONDecodeError):
        return None


def read_trace(case_dir: Path | str) -> list[dict]:
    """Read all trace events (oldest first). Skips corrupt/partial trailing line."""
    trace_path = Path(case_dir) / TRACE_FILE
    events: list[dict] = []
    if not trace_path.exists():
        return events
    with open(trace_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue  # torn write from a crash; ignore
    return events


# ---------------------------------------------------------------------------
# state.json — the ONLY writer entry point for control state
# ---------------------------------------------------------------------------

def load_state(case_dir: Path | str) -> dict | None:
    """Load state.json for a case; None when absent."""
    return read_json_or_none(Path(case_dir) / STATE_FILE)


def write_state(
    case_dir: Path | str,
    state: dict,
    *,
    transition: tuple[str, str] | None = None,
    verified: str | None = None,
    verified_artifacts: dict[str, str] | None = None,
    now: Callable[[], str] | None = None,
) -> str:
    """Atomically write state.json (schema: contracts/state-and-artifacts.md §1).

    Applies state-first transition discipline: the caller validates first, then
    calls write_state. When a *transition* (from, to) is given, a checkpoint is
    appended to ``state["checkpoints"]`` (append-only, one per verified
    transition) and a detailed checkpoint record — including the full
    ``verified_artifacts`` path→sha256 map used by resume — is written to
    ``history/checkpoint-<n>.json``. Does NOT append trace events itself.

    Returns the sha256 of the written state bytes.
    """
    case_dir = Path(case_dir)
    now = now or utc_now_iso
    state = dict(state)
    state["updated_at"] = now()
    state_bytes = json.dumps(state, indent=2, ensure_ascii=False).encode("utf-8")
    # hash the written bytes; if a checkpoint is added we rewrite with it included
    if transition is not None:
        frm, to = transition
        ck = {
            "ts": now(),
            "from": frm,
            "to": to,
            "verified": verified or "",
            "state": "pending",  # replaced below with the final state hash
        }
        state.setdefault("checkpoints", []).append(ck)
        state_bytes = json.dumps(state, indent=2, ensure_ascii=False).encode("utf-8")
        ck["state"] = "sha256:" + sha256_bytes(state_bytes)
        state_bytes = json.dumps(state, indent=2, ensure_ascii=False).encode("utf-8")
        # update the checkpoint history sidecar (schema-exact state is already stored;
        # history adds the artifact map used by resume.py)
        hist = {
            "checkpoint_index": len(state["checkpoints"]) - 1,
            "ts": ck["ts"],
            "from": frm,
            "to": to,
            "verified": ck["verified"],
            "state_sha256": ck["state"],
            "verified_artifacts": dict(verified_artifacts or {}),
        }
        atomic_write_json(case_dir / HISTORY_DIR / f"checkpoint-{len(state['checkpoints']):04d}.json", hist)
    sha = atomic_write_bytes(case_dir / STATE_FILE, state_bytes)
    return sha


def append_checkpoint(
    case_dir: Path | str,
    state: dict,
    frm: str,
    to: str,
    verified: str,
    verified_artifacts: dict[str, str] | None = None,
    now: Callable[[], str] | None = None,
) -> str:
    """Backward-compatible alias for a checkpointed state write."""
    return write_state(
        case_dir, state, transition=(frm, to), verified=verified,
        verified_artifacts=verified_artifacts, now=now,
    )


# ---------------------------------------------------------------------------
# trace.jsonl — the ONLY appender for trace events
# ---------------------------------------------------------------------------

TRACE_FIELDS = (
    "ts", "event", "case_id", "request_id", "candidate_id", "role", "round",
    "artifact", "artifact_sha256", "prompt_versions", "backend",
    "tokens_in", "tokens_out", "detail",
)

TRACE_EVENT_NAMES = frozenset({
    "CASE_CREATED", "REQUEST_BOUND", "AGENT_DISPATCHED", "AGENT_RESULT",
    "GATE_RUN", "GATE_PASS", "GATE_FAIL", "STATE_TRANSITION",
    "FINDING_OPENED", "FINDING_VERIFIED_CLOSED", "REPAIR_PACKET_ISSUED",
    "BUDGET_CHECK", "APPROVAL_REQUESTED", "APPROVAL_RECORDED", "CASE_CLOSED",
})


def append_trace(case_dir: Path | str, event: dict, now: Callable[[], str] | None = None) -> None:
    """Append one trace event (schema: contracts §2). Validates the event name."""
    name = event.get("event")
    if name not in TRACE_EVENT_NAMES:
        raise ValueError(f"unknown trace event: {name!r}")
    now = now or utc_now_iso
    ev = {k: event.get(k) for k in TRACE_FIELDS}
    ev["ts"] = event.get("ts") or now()
    # never log full prompts/responses (contract §2)
    detail = event.get("detail") or {}
    ev["detail"] = detail
    atomic_append_jsonl(Path(case_dir) / TRACE_FILE, ev)


def trace_request_count(case_dir: Path | str) -> int:
    """Number of requests bound so far (REQUEST_BOUND events), for id sequencing."""
    return sum(1 for e in read_trace(case_dir) if e.get("event") == "REQUEST_BOUND")


# ---------------------------------------------------------------------------
# request.json binding
# ---------------------------------------------------------------------------

def write_request(case_dir: Path | str, request: dict) -> str:
    """Write the currently bound request.json (case-level single active request)."""
    return atomic_write_json(Path(case_dir) / REQUEST_FILE, request)


def read_request(case_dir: Path | str) -> dict | None:
    """Read the currently bound request.json; None when absent."""
    return read_json_or_none(Path(case_dir) / REQUEST_FILE)
