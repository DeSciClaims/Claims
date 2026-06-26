# Judge V2 Audit Record Fields

Judge V2 writes one compact audit row per evaluated claim to `claim_audit_records.csv`.

## Input Fields

| Field | Description |
| --- | --- |
| `--extraction-output-json` | Path to a `section_context_v1_output.json` file. |
| `--mode` | `intrinsic_audit` evaluates extracted claims directly; `gold_comparison` compares against reviewed/gold rows. |
| `--gold-reviewed-file` | Required for `gold_comparison`; CSV/XLSX with reviewed quote groups. |
| `--extraction-run-id` | Optional run ID to write into audit rows. Defaults to the parent run folder name. |
| `--output-dir` | Optional output directory. |
| `--audit-method` | `deterministic` or `llm`. Deterministic is the default; `llm` uses the same compact output schema. |

## Output Fields

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
| `overall_score` | Mean of the three dimension scores, from `0.0` to `1.0`. |
| `complete_coverage_score` | Score for whether meaning-critical claim parts are present. |
| `complete_coverage_comment` | Short coverage explanation. |
| `accurate_extraction_score` | Score for profile-shape and extraction consistency. |
| `accurate_extraction_comment` | Short accuracy explanation. |
| `evidence_evaluation_score` | Score for evidence presence/linking. |
| `evidence_evaluation_comment` | Short evidence explanation. |
| `primary_issue` | First issue tag, if any. |
| `issue_tags` | JSON list of detected issue tags. |
| `missing_elements` | JSON list of missing fields/elements. |
| `suggested_corrections_json` | JSON object containing machine-suggested corrections. |
| `comments` | Overall compact audit comment. |
| `gold_group_id` | Reviewed quote group ID, gold mode only. |
| `gold_source_quote` | Reviewed source quote, gold mode only. |
| `gold_match_score` | Deterministic match score, gold mode only. |
| `created_at` | UTC timestamp for the audit row. |
