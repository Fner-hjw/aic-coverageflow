"""State store primitives: checkpoint hashes and run liveness."""

import json
import os
import subprocess
import sys

from coverageflow import statestore


def test_every_trace_event_the_orchestrator_emits_is_registered():
    """An unregistered event name raises at runtime, mid-round.

    `FINDING_DROPPED` was added to the orchestrator without adding it to
    TRACE_EVENT_NAMES, so the run died inside _trace after the critics had
    already been paid for. The allowlist and the call sites have to agree.
    """
    import re
    from pathlib import Path
    from coverageflow.statestore import TRACE_EVENT_NAMES

    src = Path(__file__).resolve().parents[1] / "coverageflow" / "orchestrator.py"
    emitted = set(re.findall(r'event="([A-Z_]+)"', src.read_text()))
    assert emitted, "no trace events found; the regex has drifted"
    unknown = emitted - TRACE_EVENT_NAMES
    assert not unknown, f"unregistered trace events: {sorted(unknown)}"


def _state() -> dict:
    return {"case_id": "c", "status": "RUNNING", "checkpoints": []}


def test_the_last_checkpoint_hash_verifies_against_the_written_state(tmp_path):
    """The recorded hash must be reproducible from the file it is written to.

    It used to be the hash of the state *before* the checkpoint's own hash was
    substituted in, so it could never be verified and the field was decorative.
    """
    state = _state()
    statestore.write_state(tmp_path, state, transition=("CREATED", "RUNNING"),
                           verified="case start")
    statestore.write_state(tmp_path, state, transition=("RUNNING", "RUNNING"),
                           verified="rounds/round-001 build start")
    written = statestore.load_state(tmp_path)
    assert len(written["checkpoints"]) == 2
    assert statestore.verify_checkpoint_state(written)


def test_a_tampered_state_fails_checkpoint_verification(tmp_path):
    state = _state()
    statestore.write_state(tmp_path, state, transition=("CREATED", "RUNNING"))
    written = statestore.load_state(tmp_path)
    written["status"] = "STOPPED"
    assert not statestore.verify_checkpoint_state(written)


def test_run_owner_reports_only_a_live_lock_holder(tmp_path):
    assert statestore.run_owner(tmp_path) is None
    lock = statestore.acquire_case_lock(tmp_path)
    try:
        owner = statestore.run_owner(tmp_path)
        assert owner is not None and owner["pid"] == os.getpid()
    finally:
        statestore.release_case_lock(lock)
    assert statestore.run_owner(tmp_path) is None


def test_a_lock_left_by_a_dead_process_is_not_a_live_run(tmp_path):
    """A crashed run leaves run.lock behind; that is not a running case."""
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    (tmp_path / "run.lock").write_text(
        json.dumps({"pid": proc.pid, "ts": "2026-11-10T15:15:40Z"}))
    assert statestore.run_owner(tmp_path) is None
