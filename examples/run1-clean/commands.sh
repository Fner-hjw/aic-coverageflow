#!/usr/bin/env bash
# Evidence run 1 — clean end-to-end coverage update (Intel bundle, stub dispatch + real engines).
# Run from the repository root:   bash examples/run1-clean/commands.sh
set -euo pipefail

WS=examples/run1-clean
CASE=intc-fy2026q3-20261030
mkdir -p "$WS"

# 1) Create the case: trigger doc intc-10q-2026q3, cutoff 2026-10-30, all 12
#    hashed bundle documents in the input manifest (trigger doc listed first).
python -m coverageflow --workspace "$WS" case new \
    --ticker INTC --period fy2026q3 --trigger-doc intc-10q-2026q3 \
    --cutoff 2026-10-30 \
    --manifest "$WS/inputs-manifest.json" \
    > "$WS/out-case-new.txt"

# 2) Run the loop: build -> gates -> critics -> merge -> 2 clean reviews -> PENDING_APPROVAL.
python -m coverageflow --workspace "$WS" run --case "$CASE" \
    > "$WS/out-run.txt"

# 3) Human approval gate (auditable record; only the human publishes).
python -m coverageflow --workspace "$WS" approve --case "$CASE" \
    --decision APPROVED --by "Gaetano Marco Chiantia" \
    --reason "Quarterly update verified: all gates pass, 2 clean reviews, no open findings." \
    > "$WS/out-approve.txt"

# 4) Gate report snapshot (deterministic rerun for the record).
python -m coverageflow --workspace "$WS" gates --case "$CASE" \
    > "$WS/out-gates.txt"

echo "run1 complete: $WS/cases/$CASE"

# 5) Export the dossier (LaTeX -> PDF) from the verified evidence artifacts.
python -m coverageflow.export_dossier --case "$WS/cases/$CASE" \
    > "$WS/out-export-dossier.txt"
