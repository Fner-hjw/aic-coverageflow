

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
