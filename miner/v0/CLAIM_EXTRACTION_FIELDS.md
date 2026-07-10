# Miner v0 Claim Extraction Fields

`miner.v0` uses a staged section-context extraction flow, but writes a simpler review schema. It first extracts raw candidate spans, then classifies those spans as claim, evidence, background/assumption, method/result, mixed, or abstain. Compound candidates are split into decomposed units before final claim/evidence normalization. A final atomicity repair stage checks linked claims and splits any remaining compound claims. The final review output contains paper-owned claim text, evidence item text, links, and source provenance. Profiles, SPO triples, rich context, details, and ontology mappings are not surfaced as v0 review data.

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
| `raw_section_outputs` | Per-section debug records containing extraction decisions, candidate spans, classified spans, decomposed units, atomicity repair actions, and gated claim counts. |
| `paper_summary` | Paper-level extraction summary, if generated. |
| `section_summaries` | Section-level summaries, if generated. |

## Internal Candidate Span Fields

Candidate spans, classified spans, and decomposed units are stored under `raw_section_outputs` for debugging and validation. They are not imported as review rows.

| Field | Description |
| --- | --- |
| `candidate_id` | Section-local candidate identifier, such as `c0`. |
| `source_text` | Exact or near-exact source text from the section. |
| `initial_role_hint` | Stage-1 hint: `claim`, `evidence`, `background_assumption`, `method_result`, `mixed`, or `unclear`. |
| `primary_label` | Stage-2 label: `claim`, `evidence`, `background_assumption`, `method_result`, `mixed`, or `abstain`. |
| `rhetorical_role` | Discourse role, such as `hypothesis`, `observation`, `result`, or `conclusion`. |
| `claim_subtype` | Claim type when applicable, such as `hypothesis`, `causal`, `associational`, `mechanistic`, `comparative`, or `descriptive`. |
| `evidence_type` | Evidence type when applicable, such as `statistic`, `figure`, `table`, `observation`, `estimate`, or `dataset`. |
| `modality` | Epistemic force, such as `certain`, `probable`, `possible`, or `speculative`. |
| `polarity` | Claim/result polarity: `positive`, `negative`, `null`, or `mixed`. |
| `attribution` | Whether the span is attributed to `own_work`, `prior_literature`, `widely_accepted`, `disputed`, or `unclear`. |
| `confidence` | Model confidence for the span classification. |

## Internal Decomposed Unit Fields

Decomposed units are the bridge between classified spans and final review objects. A compound candidate can produce several decomposed units.

| Field | Description |
| --- | --- |
| `unit_id` | Section-local unit identifier, such as `u0`. |
| `source_candidate_ids` | Candidate IDs used to produce this unit. |
| `unit_text` | Atomic unit text before final claim/evidence normalization. |
| `primary_label` | `claim`, `evidence`, `background_assumption`, `method_result`, or `abstain`. |
| `rhetorical_role` | Discourse role, such as `hypothesis`, `observation`, `result`, or `conclusion`. |
| `claim_subtype` | Claim type when applicable. |
| `evidence_type` | Evidence type when applicable. |
| `modality` | Epistemic force when applicable. |
| `polarity` | Claim/result polarity when applicable. |
| `attribution` | Attribution source when applicable. |
| `confidence` | Model confidence for the decomposed unit. |

## Internal Atomicity Repair Fields

Atomicity repair records are stored under `raw_section_outputs`.

| Field | Description |
| --- | --- |
| `atomicity_repair_actions` | Repair actions returned by the final repair stage. |
| `pre_atomicity_repair` | The section claim/evidence/link objects before repair. |
| `action` | Repair action type, such as `split_claim` or `no_repair_needed`. |
| `reason` | Short explanation of the repair decision. |
| `source_claim_index` | Original claim index when a repair applies to a specific claim. |
| `new_claim_indexes` | New claim indexes produced by a split repair. |

## Evidence Item Fields

Evidence items are stored in `section_context_v1_output.json` and embedded in `evidence_items_json`.

| Field | Description |
| --- | --- |
| `evidence_id` | Stable evidence item identifier. |
| `paper_id` | Paper identifier. |
| `role` | How the evidence supports the claim. |
| `evidence_method` | Plain-text label for how the evidence supports the claim. |
| `evidence_type` | Optional evidence class, such as statistic, figure, table, observation, estimate, or dataset. |
| `rhetorical_role` | Optional discourse role, such as observation or result. |
| `outcome_type` | Optional plain-text broad evidence outcome label. |
| `presentation_type` | Plain-text presentation format, usually `text`. |
| `summary_text` | Short evidence summary. |
| `source_span_ids` | Source spans supporting the evidence item. |
| `source_candidate_ids` | Candidate span IDs used to produce the evidence item. |

## Claim JSON Fields

Claims are stored in `section_context_v1_output.json` and exported into `extracted_claims.csv`.

| Field | Description |
| --- | --- |
| `claim_id` | Stable claim identifier. |
| `paper_id` | Paper identifier. |
| `claim_text` | Atomic, self-contained claim text. |
| `claim_subtype` | Optional claim type, such as associational, causal, mechanistic, comparative, descriptive, or hypothesis. |
| `modality` | Optional epistemic force, such as certain, probable, possible, or speculative. |
| `polarity` | Optional polarity, such as positive, negative, null, or mixed. |
| `attribution` | Optional attribution, usually `own_work` for v0 review claims. |
| `source_span_ids` | Source spans supporting the claim. |
| `source_candidate_ids` | Candidate span IDs used to produce the claim. |
| `extractor_confidence` | Model confidence for the final normalized claim. |

## Field Semantics

| Category | Meaning |
| --- | --- |
| `claim_profile` | Internal extraction scaffold only. Not surfaced in v0 review output. |
| `context` | Internal extraction scaffold only. Meaning-critical qualifiers should be visible inside `claim_text` or `summary_text`. |
| `details` | Internal extraction scaffold only. Numeric/statistical payloads should be visible inside the text fields. |
| `ontology` | Internal extraction scaffold only. Not surfaced in v0 review output. |
| `source_span_ids` | Provenance pointers back to parsed paper text. Multiple spans are allowed when a claim/evidence item spans multiple sentences or sources. |
