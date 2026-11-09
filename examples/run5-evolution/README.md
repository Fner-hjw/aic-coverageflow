# run5-evolution — evolution: approve / scope-reject / rollback (3-part demo)

Reproduce (repo root): `bash examples/run5-evolution/commands.sh`

```bash
# 5a — scoped change passes everything and is approved
python -m coverageflow --workspace examples/run5-evolution evolve prepare --scope "prompt card: extractor citation formatting"
python -m coverageflow --workspace examples/run5-evolution evolve propose --spec examples/run5-evolution/specs/sys-1.1.json
python -m coverageflow --workspace examples/run5-evolution evolve regress --version sys-1.1 \
    --results examples/run5-evolution/specs/sys-1.1-regression-results.json
python -m coverageflow --workspace examples/run5-evolution evolve approve --version sys-1.1 \
    --decision APPROVED --by "Gaetano Marco Chiantia" --reason "..."
# 5b — over-broad proposal (touches LOCKED controller scope) is refused + rejected
python -m coverageflow --workspace examples/run5-evolution evolve propose --specs .../specs/sys-2.0.json
python -m coverageflow --workspace examples/run5-evolution evolve regress --version sys-2.0 --results .../specs/sys-2.0-review-failed.json
python -m coverageflow --workspace examples/run5-evolution evolve approve --version sys-2.0 \
    --decision APPROVED ...     # REFUSED: exit 1 (regression/scope failed)
python -m coverageflow --workspace examples/run5-evolution evolve approve --version sys-2.0 \
    --decision REJECTED --by "Gaetano Marco Chiantia" --reason "Scope gate: locked controller logic."
# 5c — second approved version, then authorized rollback + verification case
python -m coverageflow --workspace examples/run5-evolution evolve propose --spec .../specs/sys-1.2.json
python -m coverageflow --workspace examples/run5-evolution evolve regress --version sys-1.2 --results .../sys-1.2-regression-results.json
python -m coverageflow --workspace examples/run5-evolution evolve approve --version sys-1.2 ... # active = sys-1.2
python -m coverageflow --workspace examples/run5-evolution evolve rollback --version sys-1.1 ...
# verification case under the rolled-back active version (see commands.sh)
```

(The exact commands with full paths are in `commands.sh`; the strings above
abbreviate paths for readability.)

What to look at:
- `versions/registry.json` — the four registry entries: sys-1.0 (baseline),
  sys-1.1 APPROVED (was the active pointer), **sys-2.0 REJECTED** with the
  scope-violation review record (fresh_critic_findings names the locked
  controller files; `regressions_all_pass: false`), sys-1.2 APPROVED with
  `rollback_to: sys-1.1`.
- `out-5b-approve-refused.txt` — the registry gate refuses the approve
  attempt with exit code 1 ("cannot approve a version whose fresh-critic
  review did not pass all regressions") and the human REJECT that follows.
- `out-5c-rollback.txt` — `active: sys-1.1` again after authorized rollback.
- `cases/intc-fy2026q3-20261030/state.json` — the verification case created
  after the rollback pins the rolled-back configuration
  (`config_versions`: extractor 1.1 / engines 1.0) and reached CLOSED via
  the full acceptance path (see `out-5c-verification-*.txt`).
- `specs/` — the recorded proposals and regression evidence for each version.
