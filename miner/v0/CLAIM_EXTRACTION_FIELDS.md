# Miner v0 Claim Extraction Fields

`miner.v0` uses the section-context extraction flow, but writes a simpler review schema. It extracts paper-owned claim text, evidence item text, and source provenance. Profiles, SPO triples, context, details, and ontology mappings are internal compatibility scaffolding only and are not surfaced as v0 review data.

## Input Fields

| Field | Description |
| --- | --- |
| `--artifact-json` | Path to an `artifact.json` containing paper metadata, sections, spans, and parsed text. |
| `SUBNET_CLAIMS_RUN_LABEL` | Optional run label used to name the output folder. |
| `--output-dir` | Optional output directory override, if supported by the runner. |

## Output Files

| File | Rows | Purpose |
| --- | ---: | --- |
| `section_context_v1_output.json` | 1 | Full extraction output for the paper, using the v0 simplified claim/evidence payload. |
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
| `claim_profile` | Compatibility column kept for import tooling. Blank in v0 review output. |
| `claim_text` | Natural-language claim statement. |
| `subject` | Compatibility column kept for import tooling. Blank in v0 review output. |
| `predicate` | Compatibility column kept for import tooling. Blank in v0 review output. |
| `object` | Compatibility column kept for import tooling. Blank in v0 review output. |
| `context_summary` | Usually empty in v0. |
| `context_json` | Usually `{}` in v0. |
| `details_summary` | Usually empty in v0. |
| `details_json` | Usually `{}` in v0. |
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
| `claims` | Extracted paper-owned claim texts plus source provenance. |
| `evidence_items` | Evidence text records that support claims. |
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
| `evidence_method` | Plain-text label for how the evidence supports the claim. |
| `outcome_type` | Optional plain-text broad evidence outcome label. |
| `presentation_type` | Plain-text presentation format, usually `text`. |
| `summary_text` | Short evidence summary. |
| `source_span_ids` | Source spans supporting the evidence item. |

## Field Semantics

| Category | Meaning |
| --- | --- |
| `claim_profile` | Internal extraction scaffold only. Not surfaced in v0 review output. |
| `context` | Internal extraction scaffold only. Meaning-critical qualifiers should be visible inside `claim_text` or `summary_text`. |
| `details` | Internal extraction scaffold only. Numeric/statistical payloads should be visible inside the text fields. |
| `ontology` | Internal extraction scaffold only. Not surfaced in v0 review output. |
| `source_span_ids` | Provenance pointers back to parsed paper text. Multiple spans are allowed when a claim/evidence item spans multiple sentences or sources. |
