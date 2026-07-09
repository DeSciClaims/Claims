# Validator v0 Audit Fields

Validator v0 is the simplified Judge V2 flow for miner v0 outputs. It keeps the compact CSV shape used by the review/upload tools, but the scoring target is now a flat claim-evidence pair:

- Is `claim_text` a faithful claim made by this paper?
- Does every claim have evidence item text?
- Does the cited evidence exist in, or clearly ground to, the source span/section?
- Does the run cover the paper's important contribution claims across relevant sections?

It does not require SPO fields, ontology mappings, rich context, or details. Some legacy columns remain for compatibility and are expected to be empty in v0.

## Input Fields

| Field | Description |
| --- | --- |
| `--extraction-output-json` | Path to a miner v0 `section_context_v1_output.json`-style file. |
| `--mode` | `intrinsic_audit` evaluates extracted claim-evidence pairs against the paper/output itself; `gold_comparison` compares against reviewed/gold rows. |
| `--gold-reviewed-file` | Required for `gold_comparison`; CSV/XLSX with reviewed quote groups. |
| `--extraction-run-id` | Optional run ID written into audit rows. Defaults to the parent run folder name. |
| `--output-dir` | Optional output directory. |
| `--audit-method` | `deterministic` or `llm`. `llm` is preferred when judging semantic grounding, prior-work attribution, and evidence quality. |

## Output Files

| File | Rows | Purpose |
| --- | ---: | --- |
| `run_audit_record.csv` | 1 | Holistic validator audit for the extraction run. |
| `claim_audit_records.csv` | 0..N | Claim-level diagnostics for each evaluated claim-evidence pair. |
| `candidate_missing_claims.csv` | 0..N | Intrinsic LLM candidates for important paper claims missing from the extraction. |
| `weak_or_unsupported_claims.csv` | 0..N | Intrinsic diagnostics for claims with weak, missing, or unsupported evidence. |
| `missing_gold_claims.csv` | 0..N | Gold mode only. Gold claims not found in the extraction. |
| `extra_extracted_claims.csv` | 0..N | Gold mode only. Extracted claims with no adequate gold match. |

## Run Output Fields

`run_audit_record.csv`

| Field | Description |
| --- | --- |
| `paper_id` | Paper ID. |
| `extraction_run_id` | Extraction run being audited. |
| `audit_source` | Always `validator`. |
| `audit_mode` | `intrinsic_audit` or `gold_comparison`. |
| `audit_method` | `deterministic` or `llm`. |
| `audit_version` | Audit schema/version. |
| `audit_status` | `accepted`, `needs_correction`, or `rejected`. |
| `n_claims` | Number of claim audit rows. |
| `n_accepted` | Number of claim rows accepted. |
| `n_needs_correction` | Number of claim rows needing correction. |
| `n_rejected` | Number of claim rows rejected. |
| `n_gold_claims` | Gold mode only. Number of expected gold claims. |
| `n_gold_claims_matched` | Gold mode only. Number of matched gold claims. |
| `n_gold_claims_missing` | Gold mode only. Number of gold claims not matched. |
| `n_extra_extracted_claims` | Gold mode only. Number of extracted claims without a gold match. |
| `n_candidate_missing_claims` | Intrinsic mode only. Number of candidate missing claims from full-paper audit. |
| `n_weak_or_unsupported_claims` | Intrinsic mode only. Number of weak or unsupported extracted claims. |
| `overall_score` | Mean of available run dimension scores from `0.0` to `1.0`. |
| `complete_coverage_score` | Run-level coverage. In intrinsic LLM mode, penalizes high-confidence missing important claims. In deterministic intrinsic mode this may be empty/unscored. |
| `complete_coverage_comment` | Coverage explanation. |
| `accurate_extraction_score` | Run-level mean of claim-text grounding scores. |
| `accurate_extraction_comment` | Accuracy explanation. |
| `evidence_evaluation_score` | Run-level mean of evidence support scores. |
| `evidence_evaluation_comment` | Evidence explanation. |
| `primary_issue` | Most important recurring issue tag. |
| `issue_tags` | JSON list of recurring issue tags. |
| `missing_elements` | JSON list of missing fields/elements. |
| `comments` | Compact overall audit comment. |
| `created_at` | UTC timestamp. |

## Score Semantics

| Dimension | Meaning |
| --- | --- |
| `complete_coverage_score` | Did the miner capture the paper's important contribution claims across relevant sections? More high-confidence missing claims lowers this score. |
| `accurate_extraction_score` | Is each `claim_text` faithful to its source span/section or gold target, and is it a claim made by this paper rather than a prior-work/background claim? |
| `evidence_evaluation_score` | Does each claim have evidence item text, source provenance, and evidence that supports the claim? |

Deterministic scoring checks structure and provenance only. LLM scoring can judge semantic support, prior-work attribution, overstatement, and evidence quality.

## Claim Output Fields

`claim_audit_records.csv`

| Field | Description |
| --- | --- |
| `paper_id` | Paper ID. |
| `extraction_run_id` | Extraction run being audited. |
| `claim_id` | Extracted claim ID. |
| `claim_profile` | Compatibility field; usually `claim_text_v0` or inherited value. Not required for v0 scoring. |
| `claim_text` | Extracted claim text being audited. |
| `subject` | Compatibility field. May be empty in v0. |
| `predicate` | Compatibility field. May be empty in v0. |
| `object` | Compatibility field. May be empty in v0. |
| `audit_source` | Always `validator`. |
| `audit_mode` | `intrinsic_audit` or `gold_comparison`. |
| `audit_method` | `deterministic` or `llm`. |
| `audit_version` | Audit schema/version. |
| `audit_status` | `accepted`, `needs_correction`, or `rejected`. |
| `overall_score` | Mean of claim-level accuracy and evidence scores. |
| `complete_coverage_score` | Empty for claim rows; coverage is run-level. |
| `complete_coverage_comment` | Empty for claim rows. |
| `accurate_extraction_score` | Claim-level grounding/accuracy score. |
| `accurate_extraction_comment` | Short explanation of claim-text fidelity. |
| `evidence_evaluation_score` | Claim-level evidence support score. |
| `evidence_evaluation_comment` | Short explanation of evidence quality/provenance. |
| `primary_issue` | First/primary issue tag. |
| `issue_tags` | JSON list of issue tags. |
| `missing_elements` | JSON list of missing fields/elements. |
| `suggested_corrections_json` | JSON object with corrections only when directly supported. |
| `comments` | Compact claim-level audit comment. |
| `gold_group_id` | Gold mode only. Reviewed quote group ID. |
| `gold_source_quote` | Gold mode only. Reviewed source quote. |
| `gold_match_score` | Gold mode only. Match score. |
| `gold_match_status` | Gold mode only. `matched`, `partial`, `missing_gold`, or `extra_extracted`. |
| `gold_claim_text` | Gold mode only. Reference claim text. |
| `gold_subject` | Gold mode compatibility field. |
| `gold_predicate` | Gold mode compatibility field. |
| `gold_object` | Gold mode compatibility field. |
| `source_support_status` | Intrinsic mode support label: `supported`, `partially_supported`, `unsupported`, or `uncertain`. |
| `source_support_comment` | Evidence support explanation. |
| `created_at` | UTC timestamp. |

## Diagnostic Fields

`candidate_missing_claims.csv`

| Field | Description |
| --- | --- |
| `candidate_claim_text` | Candidate important paper claim missing from the extraction. |
| `candidate_subject` | Compatibility placeholder; may be empty. |
| `candidate_predicate` | Compatibility placeholder; may be empty. |
| `candidate_object` | Compatibility placeholder; may be empty. |
| `source_span_ids` | JSON list of supporting source spans. |
| `confidence` | Confidence from `0.0` to `1.0`. |
| `missing_reason` | Why the claim appears missing. |

`weak_or_unsupported_claims.csv` records extracted claims whose evidence looks weak, missing, irrelevant, or unsupported.

`missing_gold_claims.csv` and `extra_extracted_claims.csv` retain gold-mode compatibility fields for review tooling. In v0, gold alignment is based primarily on `claim_text`; SPO fields are considered only when both sides provide meaningful values.
