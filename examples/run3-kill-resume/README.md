# run3-kill-resume — kill mid-run -> resume of the pending role only

Reproduce (repo root): `bash examples/run3-kill-resume/commands.sh`

```bash
python -m coverageflow --workspace examples/run3-kill-resume case new ...
python examples/run3-kill-resume/kill_during_run.py \
    --workspace examples/run3-kill-resume --case intc-fy2026q3-20261030 \
    --kill-role extractor                  # process dies during the extractor call
python -m coverageflow --workspace examples/run3-kill-resume demo kill-stage \
    --case intc-fy2026q3-20261030 --stage extractor   # torn partial on disk
python -m coverageflow --workspace examples/run3-kill-resume resume --case intc-fy2026q3-20261030
python -m coverageflow --workspace examples/run3-kill-resume approve --case intc-fy2026q3-20261030 \
    --decision APPROVED --by "Gaetano Marco Chiantia" --reason "Recovered after kill."
python examples/run3-kill-resume/check_resume_evidence.py \
    --workspace examples/run3-kill-resume --case intc-fy2026q3-20261030
```

What to look at:
- `out-kill.txt` — the dangling request (`req-0002`) left in the trace with REQUEST_BOUND + AGENT_DISPATCHED but **no AGENT_RESULT**; state stays RUNNING, pending stage `extractor`.
- `after-kill-artifacts.json` — what was verified-complete at kill time.
- `discarded/facts.json.00.partial` — the torn partial, forensically moved out of the way by the resume (never adopted as verified output).
- `out-evidence-check.txt` — the checks: completed artifacts unchanged (modulo the candidate-id header stamp the candidate stage always applies); dispatch counts: ingest/extractor-with-result/model/writer/lead = 1 each, the extractor is the only role with 2 dispatch attempts; acceptance still completed with 2 clean reviews (`ALL_CHECKS_PASS: true`).
