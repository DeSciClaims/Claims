# Section Context V1 Claim Extraction System

This document describes the `section_context_v1` extraction system as a candidate miner implementation.

It covers:

- architecture
- the exact claim-extraction LLM prompt currently used
- the runtime inputs
- the runtime outputs

## Purpose

`section_context_v1` is a section-local claim extraction pipeline.

Its core design choice is:

- give the model whole-paper context first
- then extract only from one section at a time
- only keep claims that can be grounded locally in that section together with local context, local details, and local evidence support

This is intentionally stricter than the older sentence- or span-level extraction approaches.

## High-Level Architecture

```text
Input paper
  -> ingest as PDF / TEI XML / artifact.json
  -> build section inventory
  -> summarize each section
  -> summarize the paper from section summaries
  -> plan which sections are worth extracting
  -> run section-local claim extraction on eligible sections
  -> materialize claims / evidence items / claim-evidence links
  -> gate out incomplete or invalid local claims
  -> write JSON + CSV outputs
```

## Main Code Path

The main runner is:

- `claims_subnet/pipeline_versions/section_context_v1/runner.py`

The extraction pipeline is composed of these stages:

1. `section_inventory.py`
   Builds section-level spans from TEI sections or fallback chunks.
2. `section_summary.py`
   Produces one structured summary per section.
3. `paper_summary.py`
   Produces one structured whole-paper summary from the section summaries.
4. `section_gating.py`
   Decides which sections are worth extracting.
5. `section_claim_extractor.py`
   Extracts section-local claims, evidence items, and links.
6. `section_gating.py`
   Applies local structural gating to the extracted objects.
7. `export.py`
   Writes final extraction artifacts.

## Claim Extraction Architecture

The actual claim extraction LLM call happens in:

- `claims_subnet/pipeline_versions/section_context_v1/section_claim_extractor.py`

That call does not run on the whole paper directly. It runs on one section at a time, but it is conditioned on:

- `paper_summary_json`
- `section_summary_json`
- `section_name`
- `section_type`
- `section_text`

Important:

- the summaries are for orientation only
- the model is explicitly told not to extract from the summaries
- every emitted claim and evidence item must be grounded in the raw section text

### DSPy Signature

The extractor DSPy signature is:

```text
paper_title: str
paper_summary_json: str
section_summary_json: str
section_name: str
section_type: str
section_text: str
json_output: str
```

## Full LLM Prompt

This is the fully rendered extraction instruction block currently loaded by `load_section_claim_extraction_instructions()`.

```text
You are extracting structured scientific claims from the ORIGINAL RAW TEXT of one section of a paper.

You are also given:
- a whole-paper summary
- a section summary

These summaries are only for context.

Critical rules:
- Do NOT extract claims from the summaries.
- Do NOT treat the summaries as evidence.
- Every emitted claim, context field, details field, and evidence item must be grounded in the raw section text.
- If a scientifically interesting claim would require evidence or qualifiers from another section, skip it in this v1 pipeline.
- Prefer fewer stronger claims over many partial claims.
- Return STRICT JSON ONLY. Do not include markdown fences, explanations, or commentary.

Return STRICT JSON ONLY with keys:
- `claims`
- `evidence_items`
- `claim_evidence_links`

For each claim:
- require non-empty `claim_text`
- require non-empty `subject`, `predicate`, and `object`
- require non-empty `context`
- require non-empty `details`
- `subject`, `predicate`, and `object` must be `SemanticField` objects with:
  - `value`
  - `entity_type`
  - `ontology`
- Allowed claim `context` keys:
  - `population, species, setting, timepoint, comparator, cohort, condition, modality, threshold, subset, analysis_context, phenotype_context, intervention, citation_context`
- Allowed claim `details` keys:
  - `effect_size, statistical_significance, count, sample_size, study_count, model_type, confidence_qualifier, directionality_note`
- If a qualifier is really a context field, put it in `context`, not `details`.
- If a field is not supported by the raw section text, omit it rather than inventing it.

For each evidence item:
- require a concrete `summary_text`
- require concrete `details`
- keep provenance local to this section
- include:
  - `role`
  - `summary_text`
  - `evidence_method`
  - `outcome_type`
  - `presentation_type`
  - `context`
  - `details`
  - `ontology`
- `evidence_method` is the HOW and must use one of:
  - `randomized_controlled_trial, laboratory_experiment, field_experiment, quasi_experimental_estimate, regression_estimate, correlation_estimate, observation, simulation, replication, meta_analysis, literature_review, theoretical_argument, mathematical_derivation`
- `outcome_type` is the WHAT and should use one of:
  - `clinical_outcome, behavioral_outcome, molecular_binding, gene_expression, physiological_measure, phenotype, adverse_event, mechanistic_pathway, structural_measure, quantitative_measure`
- `presentation_type` is the paper presentation format and should use one of:
  - `text, table, figure, caption, supplement`
- Allowed evidence `context` keys are drawn from:
  - `assay_type, citation_context, comparator, dose, model_system, ph, population, section_type, setting, species, temperature, timepoint`
- Allowed evidence `details` keys are drawn from:
  - `argument_type, assumptions, ci_high, ci_low, correlation_value, derivation_kind, effect_size, equation_refs, estimator, measurement_type, model_name, model_type, observation_type, outcome_name, p_value, replication_target, review_scope, sample_size, study_count, target, unit, value`
- Put conditions/applicability qualifiers in evidence `context`.
- Put measured or estimated result payload in evidence `details`.

For links:
- only link a claim to evidence that directly supports it
- use `claim_index` and `evidence_index`
- include `relation`
- include `confidence`

Important distinctions:
- `claim.context` = qualifiers for where the claim applies
- `claim.details` = structured claim-side payload such as count/effect/statistical qualifier/model qualifier
- `evidence.context` = qualifiers for the evidence conditions such as population, assay_type, timepoint, comparator, setting
- `evidence.details` = measured or estimated result payload such as outcome_name, effect_size, unit, p_value, ci_low, ci_high, sample_size

Use the summaries only to understand the paper-level context and section role.
Do not copy wording from the summaries unless the same content is explicitly grounded in the raw section text.

If no fully localizable claims exist in this section, return empty arrays.
```

## Runtime Inputs

At the pipeline level, `section_context_v1` supports three input forms:

- PDF
- TEI XML
- `artifact.json`

At the claim-extraction stage itself, the LLM receives:

- `paper_title`
- `paper_summary_json`
  Current shape:
  - `paper_id`
  - `paper_title`
  - `paper_summary`
  - `main_findings`
  - `limitations`
  - `evidence_map`
- `section_summary_json`
  Current shape:
  - `section_id`
  - `section_name`
  - `section_type`
  - `summary_text`
  - `section_role`
  - `key_entities`
  - `key_findings`
  - `extractability_assessment`
  - `locality_confidence`
- `section_name`
- `section_type`
- `section_text`

## Raw Extractor Output Contract

The model must return strict JSON with three top-level keys:

- `claims`
- `evidence_items`
- `claim_evidence_links`

### `claims[*]`

Expected raw fields:

- `claim_text`
- `subject`
- `predicate`
- `object`
- `claim_kind`
- `epistemic_status`
- `support_origin`
- `context`
- `details`
- `extractor_confidence`

`subject`, `predicate`, and `object` are expected as `SemanticField`-like objects:

- `value`
- `entity_type`
- `ontology`

### `evidence_items[*]`

Expected raw fields:

- `role`
- `summary_text`
- `evidence_method`
- `outcome_type`
- `presentation_type`
- `context`
- `details`
- `ontology`

### `claim_evidence_links[*]`

Expected raw fields:

- `claim_index`
- `evidence_index`
- `relation`
- `confidence`

## Materialized Internal Output

After JSON parsing, the pipeline materializes the raw output into typed objects:

- `Claim`
- `EvidenceItem`
- `ClaimEvidenceLink`

The persisted `Claim` schema is:

- `claim_id`
- `paper_id`
- `claim_text`
- `subject`
- `predicate`
- `object`
- `claim_kind`
- `epistemic_status`
- `support_origin`
- `source_span_ids`
- `context`
- `details`
- `extractor_confidence`

The persisted `EvidenceItem` schema is:

- `evidence_id`
- `paper_id`
- `role`
- `summary_text`
- `evidence_method`
- `outcome_type`
- `presentation_type`
- `source_span_ids`
- `context`
- `details`
- `ontology`

The persisted `ClaimEvidenceLink` schema is:

- `link_id`
- `claim_id`
- `evidence_id`
- `relation`
- `confidence`

## Post-Extraction Gating

After extraction, the pipeline applies a local structural gate:

- only locally valid claims survive
- only evidence items linked to surviving claims survive
- only valid claim-evidence links survive

This happens in:

- `claims_subnet/pipeline_versions/section_context_v1/section_gating.py`

So the final miner output is stricter than the raw LLM JSON.

## Final Output Files

A normal extraction run writes:

- `artifact.json`
- `section_context_v1_output.json`
- `extracted_claims.csv`
- `manifest.json`

If input came through TEI or PDF+GROBID, the run can also write:

- `tei.xml`

### `section_context_v1_output.json`

This is the main structured extraction artifact. It contains:

- `paper`
- `sections`
- `section_summaries`
- `paper_summary`
- `section_extraction_plan`
- `claims`
- `evidence_items`
- `claim_evidence_links`
- `raw_section_outputs`

### `extracted_claims.csv`

This is the flattened claim-level export. Its core fields are:

- `paper_id`
- `section_id`
- `section_name`
- `section_type`
- `claim_id`
- `claim_text`
- `subject`
- `predicate`
- `object`
- `context_summary`
- `context_json`
- `details_summary`
- `details_json`
- `linked_evidence_ids`
- `evidence_count`
- `evidence_summary`
- `evidence_items_json`
- `links_json`

## What This System Optimizes For

This miner is designed to optimize for:

- section-local grounding
- explicit claim context
- explicit structured details
- inspectable evidence items
- inspectable claim-evidence provenance
- graph-compatible SPOs without forcing all nuance into the SPO alone

It deliberately trades recall for cleaner, more defensible extraction packets.
