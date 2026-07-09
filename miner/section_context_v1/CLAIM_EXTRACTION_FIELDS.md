# Claim Extraction Fields

`section_context_v1` turns a paper artifact into structured claim, evidence, and provenance outputs.

## Input Fields

| Field | Description |
| --- | --- |
| `--artifact-json` | Path to an `artifact.json` containing paper metadata, sections, spans, and parsed text. |
| `SUBNET_CLAIMS_RUN_LABEL` | Optional run label used to name the output folder. |
| `--output-dir` | Optional output directory override, if supported by the runner. |

## Output Files

| File | Rows | Purpose |
| --- | ---: | --- |
| `section_context_v1_output.json` | 1 | Full structured extraction output for the paper. |
| `extracted_claims.csv` | 0..N | Reviewer/import-friendly claim rows. |
| `artifact.json` | 1 | Copy of the source artifact used for the run. |
| `manifest.json` | 1 | Run metadata and output paths. |
| `tei.xml` | 1 | Parsed TEI source when generated from PDF/GROBID. |

## Claim Row Fields

`extracted_claims.csv`

| Field | Description |
| --- | --- |
| `paper_id` | Paper identifier. |
| `section_id` | Section containing the claim, inferred from source spans. |
| `section_name` | Human-readable section name. |
| `section_type` | Normalized section type, such as abstract, results, discussion. |
| `claim_id` | Stable claim identifier. |
| `claim_profile` | Profile controlling allowed context/details fields and interpretation. |
| `claim_text` | Natural-language claim statement. |
| `subject` | Short concept-like subject. |
| `predicate` | Normalized relation phrase. |
| `object` | Short concept-like object. |
| `context_summary` | Short readable summary of claim context fields. |
| `context_json` | Structured claim context and qualifiers. |
| `details_summary` | Short readable summary of claim detail fields. |
| `details_json` | Structured payload fields such as counts, effects, p-values, lags, or roles. |
| `linked_evidence_ids` | Evidence IDs linked to this claim. |
| `evidence_count` | Number of linked evidence item payloads. |
| `evidence_summary` | Short readable summary of linked evidence. |
| `evidence_items_json` | Full linked evidence item payloads. |
| `links_json` | Claim-evidence link records and link metadata. |

## Core JSON Objects

`section_context_v1_output.json`

| Object | Description |
| --- | --- |
| `paper` | Paper metadata: title, DOI, year, authors, journal, source type. |
| `sections` | Parsed paper sections and section-level text/spans. |
| `spans` | Source text spans used for provenance. |
| `claims` | Structured extracted claims. |
| `evidence_items` | Evidence records that support claims. |
| `claim_evidence_links` | Links between claims and evidence items. |
| `paper_summary` | Paper-level extraction summary, if generated. |
| `section_summaries` | Section-level summaries, if generated. |

## Evidence Item Fields

Evidence items are stored in `section_context_v1_output.json` and embedded in `evidence_items_json`.

| Field | Description |
| --- | --- |
| `evidence_id` | Stable evidence item identifier. |
| `paper_id` | Paper identifier. |
| `role` | How the evidence supports the claim. |
| `evidence_method` | Primary evidence-method controller. |
| `evidence_outcome` | Outcome/result expressed by the evidence. |
| `presentation_type` | Where/how evidence appears, such as text, table, figure, model, proof. |
| `context` | Method-specific context qualifiers. |
| `details` | Method-specific structured payload. |
| `summary_text` | Short evidence summary. |
| `source_span_ids` | Source spans supporting the evidence item. |

## Field Semantics

| Category | Meaning |
| --- | --- |
| `claim_profile` | Controls valid claim context/details keys and semantic invariants. |
| `evidence_method` | Controls valid evidence context/details keys. |
| `context` | Qualifiers that scope interpretation, such as setting, condition, modality, mechanism, or population. |
| `details` | Structured payload, especially numeric/statistical values, roles, signs, counts, and lags. |
| `source_span_ids` | Provenance pointers back to parsed paper text. Multiple spans are allowed when a claim/evidence item spans multiple sentences or sources. |
