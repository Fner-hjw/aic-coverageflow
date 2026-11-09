# run2-seeded-repair — seeded finding -> critic-verified closure

Reproduce (repo root): `bash examples/run2-seeded-repair/commands.sh`

```bash
python -m coverageflow --workspace examples/run2-seeded-repair case new \
    --ticker INTC --period fy2026q3 --trigger-doc intc-10q-2026q3 \
    --cutoff 2026-10-30 --manifest examples/run2-seeded-repair/inputs-manifest.json
python -m coverageflow --workspace examples/run2-seeded-repair demo seed-finding \
    --case intc-fy2026q3-20261030        # -> fnd-001 opened in the ledger
python -m coverageflow --workspace examples/run2-seeded-repair run --case intc-fy2026q3-20261030
python -m coverageflow --workspace examples/run2-seeded-repair approve --case intc-fy2026q3-20261030 \
    --decision APPROVED --by "Gaetano Marco Chiantia" --reason "Seeded finding closed by critic-verified repair."
```

What to look at:
- `findings.json` — `fnd-001` (demo seeded rehearsal finding) carried into
  the review and CLOSED with `verified_evidence` = the round's `report.md`
  + its real SHA-256 (closures only count when the artifact hash matches the
  current candidate).
- `trace.jsonl` — 4 FINDING_VERIFIED_CLOSED events (one per critic; each
  independently verified the repair), then the normal acceptance path:
  2 clean full reviews -> PENDING_APPROVAL -> APPROVAL_RECORDED.
- `out-seed-finding.txt` — the injected finding id.
The repair-packet variant (critic raises a finding -> REPAIR_PACKET_ISSUED ->
full rebuild -> closure) is additionally unit-tested with a scripted raising
critic in `tests/test_orchestrator.py::test_findings_open_ledger_and_packet_then_verified_closure_on_repair`.
