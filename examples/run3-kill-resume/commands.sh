#!/usr/bin/env bash
# Evidence run 3 — kill mid-run, then verified resume of the pending role only.
# Run from the repository root:   bash examples/run3-kill-resume/commands.sh
set -euo pipefail

WS=examples/run3-kill-resume
CASE=intc-fy2026q3-20261030
mkdir -p "$WS"

# 1) Create the case (same Intel trigger + cutoff as run1).
python -m coverageflow --workspace "$WS" case new \
    --ticker INTC --period fy2026q3 --trigger-doc intc-10q-2026q3 \
    --cutoff 2026-10-30 --manifest "$WS/inputs-manifest.json" \
    > "$WS/out-case-new.txt"

# 2) Kill the run mid-flight: the real Orchestrator runs with a dispatch that
#    dies during the EXTRACTOR call. State stays RUNNING, pending stage = extractor;
#    the trace shows the dangling dispatch (REQUEST_BOUND + AGENT_DISPATCHED,
#    no AGENT_RESULT). The ingest-report artifact is already verified on disk.
python examples/run3-kill-resume/kill_during_run.py \
    --workspace "$WS" --case "$CASE" --kill-role extractor \
    > "$WS/out-kill.txt"

# 3) Stamp the torn partial output the crash leaves behind (the demo command
#    writes the exact kill footprint: a truncated artifact for the pending role).
python -m coverageflow --workspace "$WS" demo kill-stage --case "$CASE" \
    --stage extractor > "$WS/out-kill-stage.txt"

# 4) Resume: discard the unverified partial, re-dispatch ONLY the pending
#    extractor role, and return saved artifacts for completed verified stages.
python -m coverageflow --workspace "$WS" resume --case "$CASE" \
    > "$WS/out-resume.txt"

# 5) Human approval.
python -m coverageflow --workspace "$WS" approve --case "$CASE" \
    --decision APPROVED --by "Gaetano Marco Chiantia" \
    --reason "Recovered after mid-run kill: only the pending extractor re-ran." \
    > "$WS/out-approve.txt"

# 6) Evidence check: completed artifacts are byte-identical across the kill
#    (digests captured by kill_during_run.py vs. the resumed case) and that
#    only the pending role re-ran.
python examples/run3-kill-resume/check_resume_evidence.py \
    --workspace "$WS" --case "$CASE" > "$WS/out-evidence-check.txt"

echo "run3 complete"
