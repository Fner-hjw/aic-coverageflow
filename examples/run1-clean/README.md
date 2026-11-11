# run1-clean — clean end-to-end coverage update

Reproduce (repo root): `bash examples/run1-clean/commands.sh`

```bash
python -m coverageflow --workspace examples/run1-clean case new \
    --ticker INTC --period fy2026q3 --trigger-doc intc-10q-2026q3 \
    --cutoff 2026-10-30 --manifest examples/run1-clean/inputs-manifest.json
python -m coverageflow --workspace examples/run1-clean run --case intc-fy2026q3-20261030
python -m coverageflow --workspace examples/run1-clean approve --case intc-fy2026q3-20261030 \
    --decision APPROVED --by "Gaetano Marco Chiantia" --reason "Quarterly update verified."
python -m coverageflow --workspace examples/run1-clean gates --case intc-fy2026q3-20261030
python -m coverageflow.export_dossier --case examples/run1-clean/cases/intc-fy2026q3-20261030
```

What to look at (in `cases/intc-fy2026q3-20261030/`):
- `trace.jsonl` — REQUEST_BOUND -> AGENT_DISPATCHED -> AGENT_RESULT for all 15 role dispatches; 1 GATE_RUN + 7 GATE_PASS; APPROVAL_REQUESTED -> APPROVAL_RECORDED; CASE_CLOSED.
- `rounds/round-001` — generator artifacts (ingest, facts with per-number citations, forecast, engine `model.json`/`valuation.json`, report.md), `numerical/gates.json` (all 7 gates pass), critic verdicts (4x clean), merged-findings for round 1 and the re-review round 2 (same candidate, streak 2 -> acceptance).
- `state.json` — CLOSED, `clean_review_rounds: 2`, budget usage.
- `approvals/apr-0001.json` — human approval record.
- `report/v1-cand-43f.md` + `export/dossier.tex|pdf` — the published note, every number rendered from the verified artifacts.
- `out-*.txt` — captured CLI output for each step.
