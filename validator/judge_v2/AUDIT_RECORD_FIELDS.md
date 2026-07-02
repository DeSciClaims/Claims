# Judge V2 Audit Fields

Judge V2 writes two audit outputs:

- `run_audit_record.csv`: one holistic validator audit row for the extraction run.
- `claim_audit_records.csv`: one compact audit row per evaluated claim for drill-down.

The two judge modes use the same core output schema:

- `intrinsic_audit`: audits the extraction against the paper/output itself when no gold set exists.
- `gold_comparison`: audits the extraction against a reviewed/gold extraction set.

The difference is how the three scores are computed. In gold mode, coverage means recall against gold claims. In intrinsic LLM mode, coverage comes from a full-paper missing-claim discovery pass. Deterministic intrinsic mode does not score coverage.

## Input Fields

| Field | Description |
| --- | --- |
| `--extraction-output-json` | Path to a `section_context_v1_output.json` file. |
| `--mode` | `intrinsic_audit` evaluates extracted claims directly; `gold_comparison` compares against reviewed/gold rows. |
| `--gold-reviewed-file` | Required for `gold_comparison`; CSV/XLSX with reviewed quote groups. |
| `--extraction-run-id` | Optional run ID to write into audit rows. Defaults to the parent run folder name. |
| `--output-dir` | Optional output directory. |
| `--audit-method` | `deterministic` or `llm`. Deterministic is the default; `llm` uses the same compact output schema. |

## Output Files

| File | Rows | Required | Purpose |
| --- | ---: | --- | --- |
| `run_audit_record.csv` | 1 | Yes | Holistic score for the extraction run. This is the primary validator audit record. |
| `claim_audit_records.csv` | 0..N | Yes | Claim-level diagnostics explaining the run score. |
| `missing_gold_claims.csv` | 0..N | Gold mode only | Gold claims not found in the extraction. |
| `extra_extracted_claims.csv` | 0..N | Gold mode only | Extracted claims with no gold match. |
| `candidate_missing_claims.csv` | 0..N | Intrinsic mode, usually LLM | Candidate important claims detected from the paper but missing from extraction. |
| `weak_or_unsupported_claims.csv` | 0..N | Intrinsic mode, usually LLM | Extracted claims whose cited evidence appears weak, irrelevant, or unsupported. |

## Run Output Fields

`run_audit_record.csv`

| Field | Description |
| --- | --- |
| `paper_id` | Paper ID. |
| `extraction_run_id` | Extraction run being audited. |
| `audit_source` | Always `validator` for Judge V2 output. |
| `audit_mode` | `intrinsic_audit` or `gold_comparison`. |
| `audit_method` | `deterministic` or `llm`. |
| `audit_version` | Audit schema/version, default `v2`. |
| `audit_status` | `accepted`, `needs_correction`, or `rejected`. |
| `n_claims` | Number of claim audit rows included in the run audit. |
| `n_accepted` | Number of claim rows accepted by the validator audit. |
| `n_needs_correction` | Number of claim rows needing correction. |
| `n_rejected` | Number of claim rows rejected. |
| `n_gold_claims` | Gold mode only. Number of gold claims expected for this paper/run. Empty in intrinsic mode. |
| `n_gold_claims_matched` | Gold mode only. Number of gold claims matched by extracted claims. Empty in intrinsic mode. |
| `n_gold_claims_missing` | Gold mode only. Number of gold claims not matched by extracted claims. Empty in intrinsic mode. |
| `n_extra_extracted_claims` | Gold mode only. Number of extracted claims without a gold match. Empty in intrinsic mode. |
| `n_candidate_missing_claims` | Intrinsic mode only. Number of candidate missing claims identified by full-paper audit. Empty if not run. |
| `n_weak_or_unsupported_claims` | Intrinsic mode only. Number of claims flagged as weakly supported or unsupported. Empty if not run. |
| `overall_score` | Mean of the three run dimension scores, from `0.0` to `1.0`. |
| `complete_coverage_score` | Run-level coverage score. In gold mode this comes from gold/reference matching. In intrinsic LLM mode this comes from full-paper missing-claim discovery. Deterministic intrinsic mode leaves this empty. |
| `complete_coverage_comment` | Coverage explanation for the run. |
| `accurate_extraction_score` | Run-level extraction accuracy score aggregated from claim audits. |
| `accurate_extraction_comment` | Accuracy explanation for the run. |
| `evidence_evaluation_score` | Run-level evidence score aggregated from claim audits. |
| `evidence_evaluation_comment` | Evidence explanation for the run. |
| `primary_issue` | Most common issue tag, if any. |
| `issue_tags` | JSON list of recurring issue tags. |
| `missing_elements` | JSON list of recurring missing fields/elements. |
| `comments` | Overall compact run audit comment. |
| `created_at` | UTC timestamp for the audit row. |

### Run Score Semantics

| Dimension | `gold_comparison` meaning | `intrinsic_audit` meaning |
| --- | --- | --- |
| `complete_coverage_score` | Did the miner capture the gold/reference claims exhaustively? Usually recall: matched gold claims divided by total gold claims, with optional weighting later. | In LLM mode, did the miner capture important paper claims exhaustively across relevant sections? The validator reads paper sections, proposes candidate missing claims, and penalizes coverage for high-confidence missing candidates. Deterministic mode leaves this unscored. |
| `accurate_extraction_score` | Are matched extracted claims faithful to the gold claims? Compares claim text, SPO, context/details, polarity, modality, numeric payloads, and profile schema. | Are extracted claims faithful to their cited spans and internally valid? Checks profile/schema validity, source-span fidelity, and possible overstatement/invention. |
| `evidence_evaluation_score` | Does extracted evidence match or sufficiently support the gold/reference evidence? | Does each claim have correct, sufficient, and well-linked evidence from the paper? Deterministic mode checks structure; LLM mode can judge support quality. |

## Claim Output Fields

`claim_audit_records.csv`

| Field | Description |
| --- | --- |
| `paper_id` | Paper ID. |
| `extraction_run_id` | Extraction run being audited. |
| `claim_id` | Extracted claim ID. |
| `claim_profile` | Extracted claim profile. |
| `claim_text` | Extracted claim text. |
| `subject` | Extracted subject. |
| `predicate` | Extracted predicate. |
| `object` | Extracted object. |
| `audit_source` | Always `validator` for Judge V2 output. |
| `audit_mode` | `intrinsic_audit` or `gold_comparison`. |
| `audit_method` | `deterministic` or `llm`. |
| `audit_version` | Audit schema/version, default `v2`. |
| `audit_status` | `accepted`, `needs_correction`, or `rejected`. |
| `overall_score` | Diagnostic mean of claim-level extraction accuracy and evidence validity, from `0.0` to `1.0`. This is not the holistic run score. |
| `complete_coverage_score` | Deprecated/empty for claim rows. Complete coverage is only scored at the run level. |
| `complete_coverage_comment` | Deprecated/empty for claim rows. |
| `accurate_extraction_score` | Claim-level diagnostic score for faithful extraction, profile/schema validity, and common extraction failure modes. |
| `accurate_extraction_comment` | Short extraction diagnostic explanation. |
| `evidence_evaluation_score` | Claim-level diagnostic score for evidence presence, validity, support, and linking. |
| `evidence_evaluation_comment` | Short evidence diagnostic explanation. |
| `primary_issue` | First issue tag, if any. |
| `issue_tags` | JSON list of detected issue tags. |
| `missing_elements` | JSON list of missing fields/elements. |
| `suggested_corrections_json` | JSON object containing machine-suggested corrections. |
| `comments` | Overall compact audit comment. |
| `gold_group_id` | Reviewed quote group ID, gold mode only. |
| `gold_source_quote` | Reviewed source quote, gold mode only. |
| `gold_match_score` | Deterministic match score, gold mode only. |
| `gold_match_status` | Gold mode only. `matched`, `partial`, `missing_gold`, or `extra_extracted`. |
| `gold_claim_text` | Gold mode only. Matched/reference claim text. |
| `gold_subject` | Gold mode only. Matched/reference subject. |
| `gold_predicate` | Gold mode only. Matched/reference predicate. |
| `gold_object` | Gold mode only. Matched/reference object. |
| `source_support_status` | Intrinsic mode only. `supported`, `partially_supported`, `unsupported`, or `uncertain`. |
| `source_support_comment` | Intrinsic mode only. Explanation of whether cited spans support the claim. |
| `created_at` | UTC timestamp for the audit row. |

## Gold Mode Diagnostic Fields

`missing_gold_claims.csv`

| Field | Description |
| --- | --- |
| `paper_id` | Paper ID. |
| `extraction_run_id` | Extraction run being audited. |
| `gold_group_id` | Gold/reviewed group ID. |
| `gold_claim_text` | Gold claim that was not matched. |
| `gold_subject` | Gold subject. |
| `gold_predicate` | Gold predicate. |
| `gold_object` | Gold object. |
| `gold_source_quote` | Gold source quote or provenance. |
| `importance` | Optional importance weight/category if available. |
| `missing_reason` | Why the claim is considered missing. |

`extra_extracted_claims.csv`

| Field | Description |
| --- | --- |
| `paper_id` | Paper ID. |
| `extraction_run_id` | Extraction run being audited. |
| `claim_id` | Extracted claim ID. |
| `claim_text` | Extracted claim text. |
| `subject` | Extracted subject. |
| `predicate` | Extracted predicate. |
| `object` | Extracted object. |
| `best_gold_match_score` | Best available match score against gold claims. |
| `extra_reason` | Why the claim is considered extra or unsupported by gold. |

## Intrinsic Mode Diagnostic Fields

`candidate_missing_claims.csv`

| Field | Description |
| --- | --- |
| `paper_id` | Paper ID. |
| `extraction_run_id` | Extraction run being audited. |
| `candidate_claim_text` | Candidate important claim detected from the paper. |
| `candidate_subject` | Candidate subject, if identified. |
| `candidate_predicate` | Candidate predicate, if identified. |
| `candidate_object` | Candidate object, if identified. |
| `source_span_ids` | JSON list of source spans supporting the candidate. |
| `confidence` | Confidence from `0.0` to `1.0`. |
| `missing_reason` | Why this appears missing from the extraction. |

`weak_or_unsupported_claims.csv`

| Field | Description |
| --- | --- |
| `paper_id` | Paper ID. |
| `extraction_run_id` | Extraction run being audited. |
| `claim_id` | Extracted claim ID. |
| `claim_text` | Extracted claim text. |
| `source_span_ids` | JSON list of cited source spans. |
| `source_support_status` | `partially_supported`, `unsupported`, or `uncertain`. |
| `support_comment` | Explanation of the evidence problem. |
