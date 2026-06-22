# Section Context Pipeline V1

## Purpose

`section_context_v1` is a new claim extraction baseline designed around one core principle:

- do not extract a claim unless the model can also localize its context, details, and evidence within the same section.

This version is intentionally conservative. It is meant to improve calibration and faithfulness before we move to:

- `section_context_v2_agentic`
- `section_context_v3_rlm`

Those later versions can relax the locality constraint by allowing search across the full paper. This `v1` should stay simple, reproducible, and comparable.

## Critical Clarification

The summaries in this pipeline are for conditioning and navigation only.

- we do **not** extract claims from section summaries
- we do **not** treat summaries as evidence
- we do **not** let the model cite summary text as provenance

Instead:

- claims are extracted from the raw section text
- context is extracted from the raw section text
- details are extracted from the raw section text
- evidence items are extracted from the raw section text

The role of paper-level and section-level summaries is only to help the model interpret a section correctly before it extracts from that section's original text.

## Why This Differs From The Current Candidate Pipeline

The current candidate claim/SPO pipeline is quote-group driven. It works well for generating graph-friendly claims and SPOs, but the reports from the loop make a repeated failure mode visible:

- the judge often says the extraction is directionally correct
- but context is missing
- and evidence linking is often absent in the final exported rows

So this new pipeline changes the unit of reasoning:

- from: local quote group first
- to: full paper understanding first, then raw section-level extraction

## Design Goals

1. Give the model paper-level understanding before claim extraction.
2. Make sections the primary span unit for extraction.
3. Only emit claims that are self-contained enough within one section.
4. Require each emitted claim to have:
   - a complete localized context
   - useful structured details
   - at least one evidence item
   - at least one claim-evidence link
5. Keep judging configurable so we can compare `judge_v1`, `judge_v2`, or no judge.
6. Version the implementation cleanly so later agentic/RLM variants can be compared against this baseline.

## Recommended Repo Structure

### Pipeline implementation

Put the reusable implementation here:

```text
claims_subnet/pipeline_versions/section_context_v1/
  __init__.py
  README.md
  config.py
  runner.py
  models.py
  section_inventory.py
  paper_summary.py
  section_summary.py
  section_claim_extractor.py
  section_gating.py
  judge_adapter.py
  export.py
  prompts/
    paper_summary_instructions.md
    section_summary_instructions.md
    section_claim_extraction_instructions.md
    section_claim_gating_instructions.md
```

### CLI / scripts

Put thin entrypoints here:

```text
claims_subnet/scripts/section_context_v1/
  run_pipeline.py
  run_reviewer_split.py
  run_loop.py
```

The rule should be:

- `scripts/...` only parse args, resolve paths, call the implementation
- `pipeline_versions/...` contains the real logic

## Core Pipeline Stages

### Stage 0: Document ingest

Reuse the existing TEI / PDF extraction path:

- `claims_subnet.extractors.tei_parser`
- existing `Span` schema from `claims_subnet.schemas.models`

Output:

- raw paragraph-level spans
- section metadata
- paper metadata

### Stage 1: Section inventory

Build a normalized section inventory for the paper.

For each section:

- `section_id`
- `section_name`
- `section_type`
- ordered paragraph span ids
- concatenated section text
- optional token count / char count

This stage should also merge tiny fragments when a section is split awkwardly by parsing noise.

Output:

- `paper_sections.json`

### Stage 2: Paper understanding pass

Before extracting claims, give the model the whole paper in structured form.

This stage should be two-step:

1. Summarize each section independently.
2. Summarize the whole paper from those section summaries.

Suggested outputs:

- `section_summary`
- `section_role`
  - background
  - methods
  - results
  - discussion
  - supplement
  - mixed
- `key_entities`
- `key_findings`
- `section_locality_confidence`
- `paper_summary`
- `paper_main_findings`
- `paper_limitations`
- `paper_evidence_map`

Important design choice:

- the whole-paper summary should be built from section summaries, not from a single giant prompt over the whole paper

That keeps token usage bounded and makes later agentic versions easier to extend.

Another important design choice:

- summaries are never the extraction substrate
- the extraction substrate is always the original section text

### Stage 3: Section eligibility pass

Each section should be screened before extraction.

We do not want claims from every section. We only want sections likely to contain extractable, localizable result claims.

For each section, classify:

- should extract claims: yes/no
- why
- likely claim density
- likely evidence density
- likely context completeness

Examples of sections to deprioritize:

- acknowledgements
- boilerplate methods with no result statement
- reference-like supplementary enumeration
- generic discussion prose that cannot support local evidence items

Output:

- `section_extraction_plan.json`

### Stage 4: Section-level claim extraction

This is the first extraction pass that emits claims.

Input to the model should include:

- paper title + metadata
- whole-paper summary
- current section summary
- current section text only
- explicit rule: do not emit a claim unless all required local support exists in this section

Critical rule:

- the model may use the paper summary and section summary to understand the section
- but every returned claim, context field, details field, and evidence item must be grounded in the raw current section text
- if something is only present in the summary and not present in the section text, it must not be emitted

Extraction requirements:

- only emit `Claim` objects that are section-local
- every claim must also return:
  - `context`
  - `details`
  - `evidence_items`
  - `claim_evidence_links`
- if a claim would require evidence from another section, skip it in `v1`

Operationally, this means the section summary is a conditioning input, while the section text is the only admissible extraction source.

This should intentionally favor recall loss over low-fidelity output.

### Stage 5: Section-level gating / filtering

After raw extraction, run a deterministic or LLM-based gate:

- remove claims with empty evidence lists
- remove claims with empty links
- remove claims whose context is too thin
- remove claims whose details are too generic
- remove claims where evidence summary is paraphrased but not localized
- remove claims whose SPO is graph-friendly but semantically under-contextualized

This stage is the hard policy boundary of `v1`.

Suggested gate:

- `require_evidence_item = true`
- `require_claim_evidence_link = true`
- `require_nonempty_context = true`
- `require_nonempty_details = true`
- `require_section_local_support = true`

### Stage 6: Optional judge pass

Judging should be configurable:

- `--judge-version none`
- `--judge-version v1`
- `--judge-version v2`

`judge_v2` is the best default because it already reasons over:

- claim text
- context
- details
- evidence linking
- graph quality

But the new pipeline should not depend on judging to enforce its extraction rules. The pipeline itself should already be conservative.

### Stage 7: Export and comparison artifacts

Outputs should be easy to compare across versions.

Recommended output folder pattern:

```text
claims_subnet/outputs/section_context_v1__<run_label>/
  manifest.json
  paper_sections.json
  paper_summary.json
  section_extraction_plan.json
  extracted_claims.json
  extracted_claims.csv
  extracted_claims.xlsx
  judged_claims.csv
  judged_claims.xlsx
  prompt_snapshot/
```

For loops:

```text
claims_subnet/outputs/section_context_v1_loop__<run_label>/
  iterations/
    iteration_001/
      train/
      val/
```

This mirrors the layout of the current loop outputs and keeps comparisons straightforward.

## Data Model Suggestions

The existing `Claim`, `EvidenceItem`, and `ClaimEvidenceLink` models can stay. We should add pipeline-specific helper structures, for example:

### `SectionRecord`

- `section_id`
- `paper_id`
- `section_name`
- `section_type`
- `span_ids`
- `text`
- `token_count`

### `SectionSummaryRecord`

- `section_id`
- `summary_text`
- `section_role`
- `key_entities`
- `key_findings`
- `extractability_assessment`

### `PaperSummaryRecord`

- `paper_id`
- `paper_summary`
- `main_findings`
- `limitations`
- `evidence_map`

### `SectionExtractionDecision`

- `section_id`
- `should_extract`
- `reason`
- `expected_claim_types`
- `expected_evidence_types`

## Prompt Design

We should separate prompts by stage rather than keep one large extraction prompt.

### `paper_summary_instructions.md`

Goal:

- produce a whole-paper understanding scaffold

### `section_summary_instructions.md`

Goal:

- summarize one section
- classify its role
- identify whether it is a good extraction target

### `section_claim_extraction_instructions.md`

Goal:

- extract only section-local claims
- require local context + details + evidence items

Critical instruction:

- if a claim is scientifically important but cannot be fully supported from this section alone, do not emit it in `v1`

### `section_claim_gating_instructions.md`

Goal:

- verify whether extracted claims satisfy the conservative policy boundary

## CLI Design

### `run_pipeline.py`

Primary one-paper or one-directory entrypoint.

Recommended args:

- `--input-paper`
- `--input-reviewed-files-dir`
- `--output-dir`
- `--model`
- `--judge-version`
- `--section-types`
- `--max-sections`
- `--xlsx`

### `run_reviewer_split.py`

Equivalent to the current replay/reviewer-file driven flow, but section-context-first.

Recommended args:

- `--input-reviewed-files-dir`
- `--reviewed-files-dir`
- `--output-dir`
- `--judge-version`

### `run_loop.py`

Loop-compatible runner.

Recommended args:

- `--iteration-root`
- `--iteration-name`
- `--split-name`
- `--judge-version`
- `--group-batch-size`

## Comparison Strategy

This new pipeline should be evaluated against the current candidate pipeline on:

1. acceptance / revise / reject rates
2. judge v2 context capture
3. evidence linking adequacy
4. proportion of rows with nonempty linked evidence
5. graph quality retention
6. per-quote best-score comparison

Important expectation:

- `section_context_v1` may emit fewer claims
- but the emitted claims should be more complete and more judge-aligned

## Conservative Policy For V1

This should be the default extraction rule set:

- do not optimize for maximum claim count
- do not emit claims without evidence items
- do not emit claims whose context is distributed across the paper
- do not rely on discussion-only paraphrases if no local evidence item exists
- prefer fewer, stronger claims over many partial ones

## Recommended First Implementation Order

1. Reuse span ingestion and build `SectionRecord` inventory.
2. Add section summary pass.
3. Add whole-paper summary synthesized from section summaries.
4. Add section eligibility pass.
5. Add section-level conservative extractor over raw section text.
6. Add section-level gating.
7. Add judge adapter with configurable `judge_version`.
8. Add output manifest + prompt snapshots.
9. Add loop runner for train/val comparison.

## Suggested Minimal V1 Milestone

The first runnable version should support:

- one paper
- section summaries
- whole-paper summary
- extraction from results/discussion sections only, using the original section text as the source
- strict requirement for local evidence item + link
- optional `judge_v2`

That is enough to generate the first benchmarkable baseline before we add agentic retrieval.
