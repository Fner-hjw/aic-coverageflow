# run4-perturbation — cutoff / no-leakage perturbation test

Reproduce (repo root): `bash examples/run4-perturbation/commands.sh`

```bash
python -m coverageflow --workspace examples/run4-perturbation case new ...   # baseline case, run, approve
python examples/run4-perturbation/snapshot_hashes.py --workspace ... --out hashes-baseline.json
python -m coverageflow --workspace examples/run4-perturbation demo perturb --case intc-fy2026q3-20261030
python examples/run4-perturbation/inject_post_cutoff_doc.py --workspace ... --case ...  # 8-K filed 2026-11-05
python -m coverageflow --workspace examples/run4-perturbation demo perturb --case ...    # -> leak flagged, exit 1
python examples/run4-perturbation/inject_post_cutoff_doc.py ... --restore                # frozen manifest back
python examples/run4-perturbation/check_stability.py --workspace examples/run4-perturbation
```

What to look at:
- `out-perturb-clean.txt` — `deterministic_recompute: true` (the gate engine re-runs byte-identically) and `no_post_cutoff_documents: true`.
- `out-perturb-leak.txt` — with the post-cutoff 8-K (filed 2026-11-05 > cutoff 2026-10-30) injected, the gate flags it by document id and the command exits non-zero: cutoff enforcement is structural (a gate), not a prompt request. With such a manifest a case could never pass creation.
- `out-stability-check.txt` — after the manifest is restored, all 7 published artifacts are byte-identical to the baseline snapshot (`STABLE_OUTPUTS: true`); the rejected document never reached any output.
