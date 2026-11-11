# Frozen Contracts — State & Artifacts (v1.0)

**These schemas are frozen for parallel streams A1–A5. Any change requires a new
contract version agreed by the coordinator.** All files are UTF-8 JSON. All writes
are atomic (write temp file, `os.replace`). All monetary/percentage values are plain
numbers (USD millions unless the field name says otherwise). All IDs are strings.

## Paths

```
workspace root = repo /  cases/  ... run dirs below are <case_id>/
cases/<case_id>/
  mandate.json            inputs/ + inputs/manifest.json
  request.json            rounds/round-NNN/{generator,numerical,critic}/...
  findings.json           state.json   trace.jsonl   result.json
  approvals/<decision-id>.json
  report/v<N>-<candidate8>.md          export/{model.xlsx,dossier.pdf,dossier.html}
  engine/                 replay/                 history/
versions/registry.json    prompts/ (cards)       references/ (pinned knowledge)
```
`case_id` = `<ticker-lower>-<period>-<yyyymmdd>` e.g. `intc-fy2026q3-20261030`.

## 1. state.json (ONLY the orchestrator writes this)

```json
{
  "case_id": "intc-fy2026q3-20261030",
  "status": "CREATED|RUNNING|PENDING_APPROVAL|APPROVED|REJECTED|BLOCKED|EXHAUSTED|STOPPED|CLOSED",
  "pending": {
    "role": "generator|critic|approval",          // which team is next
    "request_id": "req-0007",
    "round": 2,                                    // revision round counter (001-based in dirs)
    "retry_count": 0,                              // invalid-submission retries at this role
    "case_clock_started": "2026-11-09T21:38:04Z",
    "deadline": "2026-11-14T21:38:04Z"              // approval expiry (PENDING_APPROVAL only)
  },
  "candidate": {
    "candidate_id": "cand-a3f9...",                  // changes on ANY candidate artifact change
    "clean_review_rounds": 1,                       // consecutive clean full reviews
    "built_round": 2                                // round whose generator output is current
  },
  "budget": {"rounds_max": 4, "retries_max": 3, "case_minutes": 45,
             "role_tokens_max": 250000,
             "used": {"rounds": 2, "elapsed_minutes": 12.4, "tokens_by_role": {"extractor": 38210}}},
  "inputs_manifest_ref": "inputs/manifest.json",    // + sha256 recorded
  "config_versions": {"prompt_cards": {"ingest": "1.0", "extractor": "1.0", "model": "1.0",
                        "writer": "1.0", "critics": ["1.0"]},
                      "mapping_tables": "1.0", "engines": "1.0", "schema": "1.0"},
  "checkpoints": [                                  // append-only, one per verified transition
    {"ts": "2026-11-09T21:40:11Z", "from": "RUNNING", "to": "RUNNING",
     "verified": "round-002/generator/candidate-manifest.json", "state": "sha256:..."}
  ],
  "reason": null,                                   // terminal/human/orchestrator reason
  "updated_at": "2026-11-09T21:41:55Z"
}
```

Two rules the file cannot express on its own:

- **`checkpoints[].state`** is the hash defined by
  `statestore.checkpoint_state_hash`: the state as written, with that
  checkpoint's own `state` field blanked. A checkpoint cannot contain the hash
  of the bytes that contain it, so this is not the sha256 of `state.json`.
  Only the last checkpoint can be checked against the current file, because
  every later transition rewrites the content the earlier hashes were taken
  over.
- **`status: "RUNNING"` is a claim about the file, not about a process.** The
  orchestrator holds `run.lock` for the duration of a run, so a RUNNING case
  with no live lock holder was interrupted (crash, restart, or a state file
  edited outside the orchestrator). Readers must report it as interrupted, not
  as in progress.

## 2. trace.jsonl (one event per line)

Every event: `{"ts","event","case_id","request_id","candidate_id","role","round",
"artifact":"path","artifact_sha256","prompt_versions",{},"backend","tokens_in","tokens_out",
"detail": {...}}`. Event names fixed (see design-decisions §3). Only the orchestrator appends.
The trace never logs full prompts/responses (those live in `replay/` when recorded).

## 3. mandate.json (written at case creation; IMMUTABLE for the case)

```json
{"mandate_id": "man-0003", "case_id": "...", "ticker": "INTC", "cik": "0000050863",
 "cutoff_date": "2026-10-30", "report_policy": {"note_type": "quarterly_update",
   "length_words": [800, 1600], "sections": ["Summary","Results","Segment detail","Outlook","Valuation","Risks"]},
 "assumptions": {                                   // human-owned, dated, hashed
   "as_of": "2026-10-30",
   "wacc": 0.104, "terminal_growth": 0.025, "exit_multiple_ebitda": 14.0,
   "discount_convention": "year_end", "tax_rate": 0.15,
   "forecast_bounds": {"segments": {"CCG": [-0.10, 0.12], "DCAI": [-0.05, 0.25],
                                     "NEX": [-0.05, 0.20], "Foundry": [-0.05, 0.30],
                                     "Altera": [-0.10, 0.15], "Mobileye": [-0.05, 0.20]},
                        "margin_floor_gross": 0.30, "margin_ceiling_gross": 0.55},
   "peer_block": {"as_of": "2026-10-30",
     "peers": [{"ticker":"AMD","p_e_ttm":null,"ev_ebitda":null,"p_s":null,
                "rev_growth_yoy":null,"notional_note":"filled at mandate creation"}]},
   "dcf_comps_tolerance": 0.25},
 "limits": {"role_tokens_max": 250000, "case_minutes": 45, "rounds_max": 4, "retries_max": 3,
              "approval_hours": 48},
 "schema_version": "1.0"}
```

## 4. inputs/manifest.json

```json
{"as_of_cutoff": "2026-10-30",
 "documents": [
   {"doc_id": "intc-10q-2026q3", "form": "10-Q", "filed": "2026-10-30", "period": "2026-09-26",
    "path": "inputs/intc-10q-2026q3.htm", "sha256": "...", "bytes": 1834512,
    "source_url": "https://www.sec.gov/Archives/..."}],
 "bundle_id": "intc-2026-10", "bundle_sha256": "sha256 of ordered doc hashes"}
```

## 5. Generator team artifacts

**ingest-report.json** (Data & Ingest):
```json
{"role": "ingest", "request_id": "...", "candidate_id": "...",
 "doc_id": "...", "classification": {"form": "10-Q", "affects": ["extraction","model","valuation","report"],
   "reason": "new quarterly statements supersede prior-period estimates"},
 "slices": [{"slice_id": "intc-10q-2026q3/IS", "kind": "income_statement",
             "path": "inputs/slices/intc-10q-2026q3/IS.txt",
             "sha256": "...", "anchors": {"first_line": "Consolidated Statements of Operations"}}],
 "superseded": [{"doc_id": "...", "stage": "model", "reason": "..."}],
 "conformance": "schema 1.0", "prompt_version": "1.0"}
```

**facts.json** (Extraction; see also runtime/model schema):
```json
{"role": "extractor", "request_id": "...", "candidate_id": "...",
 "facts": [
   {"field": "income_statement.revenue", "period": "2026-Q3", "value": 13500.0,
    "unit": "USDmm", "confidence": 0.98,
    "citation": {"doc_id": "intc-10q-2026q3", "slice_id": "intc-10q-2026q3/IS",
                 "anchor": "Consolidated Statements of Operations", "locator": "line 14"}},
   ... /* ~80 fields per period, per docs/design-decisions §4 */],
 "missing": [{"field": "...", "reason": "not disclosed in slice"}],
 "notes": ["restructuring charge disclosed in MD&A but not in IS slice"],
 "conformance": "schema 1.0", "prompt_version": "1.0"}
```

**forecast_assumptions.json** (Model specialist proposal — bounded by mandate):
```json
{"role": "model", "horizon_years": 5,
 "segments": [{"segment": "DCAI", "growth": [0.18, 0.16, 0.14, 0.12, 0.10],
               "drivers": [{"text": "Q3'26 segment table", "citation": {...}}]}],
 "margin_glide": {"gross": [0.40, 0.41, 0.42, 0.42, 0.43]},
 "terminal_basis": "mandate", "conformance": "schema 1.0", "prompt_version": "1.0"}
```

**model.json / valuation.json** (deterministic engines — headers uniform):
```json
{"artifact": "model", "engine": "model_engine@1.0", "input_facts_sha256": "...",
 "candidate_id": "...", "cutoff": "2026-10-30",
 "series": {"income_statement.revenue": {"2025-Q3": 12870.0, "2026-Q3": 13500.0,
               "yoy": 0.049, "yoy_computed": true}},
 "mapping_tables": "1.0"}
```
`valuation.json` additionally: `dcf` (per-year FCF, PV, terminal both methods, EV→equity→per-share),
`comps` (peer block applied), `sensitivity` (WACC × terminal growth grid), `cross_check_delta`
(+ note explaining the delta for humans).

**candidate-manifest.json** (Generator lead; one per round):
```json
{"request_id": "...", "candidate_id": "...", "round": 2,
 "reports": [{"role": "ingest", "artifact": "...", "sha256": "..."}, ...],
 "evidence_index": "evidence sha → producing artifact"} ,
 "scope_statement": "full rebuild; stages 1-3 hash-identical to round-001 (proofs included)"}
```

## 6. Critic artifacts

**critic report (each of C1..C4)**:
```json
{"role": "critic_c1", "request_id": "...", "candidate_id": "...", "round": 2,
 "verdict": "clean|findings",
 "findings": [{"finding_id": "fnd-011", "severity": "major|minor",
   "summary": "...", "evidence": "...", "affected_artifacts": ["report.md"],
   "required_evidence": "recomputed gross margin row matching facts"}],
 "coverage": {"rubric_version": "1.0", "areas_checked": ["citation_integrity", ...]},
 "conformance": "schema 1.0", "prompt_version": "1.0"}
```

**merged-findings.json** (Critic lead; conflicts kept):
```json
{"round": 2, "candidate_id": "...",
 "findings": [/* union, each with source_critic, dedup_key, conflict_group if C1 and C2 disagree */],
 "clean": false, "coverage_ok": true, "reviewer_count": 4,
 "fresh_context_proofs": [/* per-critic dispatch trace refs — visible in trace.jsonl */]}
```

**Repair packet (written by orchestrator)** `rounds/round-NNN/repair-packet.json`:
open findings + required evidence + unchanged criteria + delta-scope statement.

## 7. Approval records & result

```json
{"decision_id":"apr-0001","case_id":"...","candidate_id":"...",
 "decision":"APPROVED|REJECTED","by":"Gaetano Marco Chiantia",
 "ts":"...","reasoning":"...","expected_evidence_refs": [...]}
```
```json
{"case_id":"...","terminal":"APPROVED|REJECTED|BLOCKED|EXHAUSTED|STOPPED",
 "candidate_id":"...","report_version":"v3-<candidate8>",
 "reason":"human approved; 2 clean reviews; all gates pass",
 "budget_final": {...}, "closed_ts":"..."}
```

## 8. Findings ledger (findings.json)

Append-only per case: `{finding_id, first_seen_round, last_seen_round, status: open|closed,
closed_by: {"round": 3, "critic": "c2", "verified_evidence": "artifact+sha"},
severity, summary, affected_artifacts}`.

## 9. Version registry (versions/registry.json)

```json
{"active": "sys-1.2",
 "versions": [{"version": "sys-1.2", "parent": "sys-1.1", "scope": "prompt card extractor",
   "proposal": {"author": "evo-change-02", "diff_files": [...], "diff_sha256": "..."},
   "tested": {"model": "...", "prompt_cards": {...}, "engines": "...", "cases": ["dev-1","dev-2","dev-3","dev-4"]},
   "review": {"fresh_critic_findings": [...], "regressions_all_pass": true,
              "identical_outputs_proof": "hashes for unchanged cases"},
   "approval": {"decision": "APPROVED", "by": "...", "ts": "..."},
   "rollback_to": null}]}
```
