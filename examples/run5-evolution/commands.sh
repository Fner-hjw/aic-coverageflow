#!/usr/bin/env bash
# Evidence run 5 — evolution: 3-part demo over the version registry.
#   5a  scoped change -> regression pass -> human APPROVE (new active version)
#   5b  over-broad proposal (touches LOCKED scope) -> review fails ->
#       approve is refused by the registry gate -> human REJECT recorded,
#       active version unchanged
#   5c  second approved version, then authorized ROLLBACK with a
#       verification case run under the restored version
# Run from the repository root:   bash examples/run5-evolution/commands.sh
set -euo pipefail

WS=examples/run5-evolution
mkdir -p "$WS/specs"

# -- 5a: a properly scoped change (extractor prompt card refinement) -------
python -m coverageflow --workspace "$WS" evolve prepare \
    --scope "prompt card: extractor citation formatting" \
    > "$WS/out-5a-prepare.txt"

python -m coverageflow --workspace "$WS" evolve propose \
    --spec "$WS/specs/sys-1.1.json" > "$WS/out-5a-propose.txt"

python -m coverageflow --workspace "$WS" evolve regress \
    --version sys-1.1 --results "$WS/specs/sys-1.1-regression-results.json" \
    > "$WS/out-5a-regress.txt"

python -m coverageflow --workspace "$WS" evolve approve \
    --version sys-1.1 --decision APPROVED --by "Gaetano Marco Chiantia" \
    --reason "Scoped prompt-card change; baseline regression identical on all 4 dev cases." \
    > "$WS/out-5a-approve.txt"

# -- 5b: over-broad proposal (touches LOCKED controller scope) ---------------
python -m coverageflow --workspace "$WS" evolve propose \
    --spec "$WS/specs/sys-2.0.json" > "$WS/out-5b-propose.txt"

# the fresh-critic review records a LOCKED-SCOPE violation and failing
# regressions; approval MUST be refused by the registry gate:
python -m coverageflow --workspace "$WS" evolve regress \
    --version sys-2.0 --results "$WS/specs/sys-2.0-review-failed.json" \
    > "$WS/out-5b-regress.txt"

python -m coverageflow --workspace "$WS" evolve approve \
    --version sys-2.0 --decision APPROVED --by "Gaetano Marco Chiantia" \
    > "$WS/out-5b-approve-refused.txt" 2>&1 \
    || echo "approve_REFUSED_exit_code=$?" >> "$WS/out-5b-approve-refused.txt"

# the human decision that goes on the record is REJECT; active stays sys-1.1:
python -m coverageflow --workspace "$WS" evolve approve \
    --version sys-2.0 --decision REJECTED --by "Gaetano Marco Chiantia" \
    --reason "Scope gate: proposal modifies locked controller logic (gates/acceptance)." \
    >> "$WS/out-5b-approve-refused.txt"

# -- 5c: second approved version, then authorized rollback --------------------
python -m coverageflow --workspace "$WS" evolve propose \
    --spec "$WS/specs/sys-1.2.json" > "$WS/out-5c-propose.txt"

python -m coverageflow --workspace "$WS" evolve regress \
    --version sys-1.2 --results "$WS/specs/sys-1.2-regression-results.json" \
    > "$WS/out-5c-regress.txt"

python -m coverageflow --workspace "$WS" evolve approve \
    --version sys-1.2 --decision APPROVED --by "Gaetano Marco Chiantia" \
    --reason "Engine default config tweak; regressions identical." \
    > "$WS/out-5c-approve.txt"

python -m coverageflow --workspace "$WS" evolve rollback \
    --version sys-1.1 --by "Gaetano Marco Chiantia" \
    --reason "Authorized rollback: verification falls back to the prior approved version." \
    > "$WS/out-5c-rollback.txt"

# -- verification run under the rolled-back active version -------------------
python -m coverageflow --workspace "$WS" case new \
    --ticker INTC --period fy2026q3 --trigger-doc intc-10q-2026q3 \
    --cutoff 2026-10-30 --manifest "$WS/inputs-manifest.json" \
    > "$WS/out-5c-case-new.txt"
python -m coverageflow --workspace "$WS" run \
    --case intc-fy2026q3-20261030 > "$WS/out-5c-verification-run.txt"
python -m coverageflow --workspace "$WS" approve --case intc-fy2026q3-20261030 \
    --decision APPROVED --by "Gaetano Marco Chiantia" \
    --reason "Verification case under rolled-back active version sys-1.1." \
    > "$WS/out-5c-verification-approve.txt"

echo "run5 complete"
