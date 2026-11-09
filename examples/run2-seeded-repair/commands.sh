#!/usr/bin/env bash
# Evidence run 2 — seeded finding -> repair loop -> critic-verified closure.
# Run from the repository root:   bash examples/run2-seeded-repair/commands.sh
set -euo pipefail

WS=examples/run2-seeded-repair
CASE=intc-fy2026q3-20261030
mkdir -p "$WS"

# 1) Create the case (same Intel trigger + cutoff as run1).
python -m coverageflow --workspace "$WS" case new \
    --ticker INTC --period fy2026q3 --trigger-doc intc-10q-2026q3 \
    --cutoff 2026-10-30 --manifest "$WS/inputs-manifest.json" \
    > "$WS/out-case-new.txt"

# 2) Scripted-rehearsal injection: an open finding lands in the ledger
#    (the same action the UI demo panel exposes as "seed finding").
python -m coverageflow --workspace "$WS" demo seed-finding --case "$CASE" \
    > "$WS/out-seed-finding.txt"

# 3) Run: the finding is CARRIED into the critic review; the reviewers must
#    verify the repair against the fresh candidate's artifact hashes before
#    the orchestrator may close it; acceptance still requires 2 clean reviews.
python -m coverageflow --workspace "$WS" run --case "$CASE" \
    > "$WS/out-run.txt"

# 4) Human approval.
python -m coverageflow --workspace "$WS" approve --case "$CASE" \
    --decision APPROVED --by "Gaetano Marco Chiantia" \
    --reason "Seeded finding carried, repaired and critic-verified; 2 clean reviews." \
    > "$WS/out-approve.txt"

echo "run2 complete"
