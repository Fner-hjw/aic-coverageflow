#!/usr/bin/env bash
# Evidence run 4 — cutoff / no-leakage perturbation test.
# Run from the repository root:   bash examples/run4-perturbation/commands.sh
set -euo pipefail

WS=examples/run4-perturbation
CASE=intc-fy2026q3-20261030
mkdir -p "$WS"

# 1) Baseline case: create, run to acceptance, approve.
python -m coverageflow --workspace "$WS" case new \
    --ticker INTC --period fy2026q3 --trigger-doc intc-10q-2026q3 \
    --cutoff 2026-10-30 --manifest "$WS/inputs-manifest.json" \
    > "$WS/out-case-new.txt"
python -m coverageflow --workspace "$WS" run --case "$CASE" > "$WS/out-run.txt"
python -m coverageflow --workspace "$WS" approve --case "$CASE" \
    --decision APPROVED --by "Gaetano Marco Chiantia" \
    --reason "Baseline for perturbation test." > "$WS/out-approve.txt"

# 2) Snapshot the published state (hashes) BEFORE any perturbation.
python examples/run4-perturbation/snapshot_hashes.py \
    --workspace "$WS" --case "$CASE" --out "$WS/hashes-baseline.json"

# 3) Perturbation check A (clean manifest): the gate engine recomputes
#    deterministically (two gate reports byte-equal) and no document in
#    the manifest is dated after the cutoff.
python -m coverageflow --workspace "$WS" demo perturb --case "$CASE" \
    > "$WS/out-perturb-clean.txt"

# 4) Perturbation check B (post-cutoff document INJECTED): the same case,
#    with a deliberately late SEC filing (filed 2026-11-05 > cutoff
#    2026-10-30) appended to the input manifest. The gate must flag it -
#    proving cutoff enforcement is structural, not a prompt request.
python examples/run4-perturbation/inject_post_cutoff_doc.py \
    --workspace "$WS" --case "$CASE"
python -m coverageflow --workspace "$WS" demo perturb --case "$CASE" \
    > "$WS/out-perturb-leak.txt" || echo "exit_code_nonzero=$?" >> "$WS/out-perturb-leak.txt"

# 5) Restore the frozen manifest and re-check stability: outputs must be
#    byte-identical to the baseline snapshot.
python examples/run4-perturbation/inject_post_cutoff_doc.py \
    --workspace "$WS" --case "$CASE" --restore
python -m coverageflow --workspace "$WS" demo perturb --case "$CASE" \
    > "$WS/out-perturb-restored.txt"
python examples/run4-perturbation/snapshot_hashes.py \
    --workspace "$WS" --case "$CASE" --out "$WS/hashes-after.json"
python examples/run4-perturbation/check_stability.py \
    --workspace "$WS" > "$WS/out-stability-check.txt"

echo "run4 complete"
