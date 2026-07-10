# Miner V0 Claim Extraction System

This document describes the `miner.v0` claim extraction system.

It covers:

- architecture
- staged claim/evidence extraction
- runtime inputs
- runtime outputs
- internal debug records

## Purpose

`miner.v0` is a section-local claim-evidence extraction pipeline for the Claims subnet.

Its core design choices are:

- give the model whole-paper and section context before extraction
- extract only from one section at a time
- first identify raw candidate spans
- then classify candidate spans as claim, evidence, background/assumption, method/result, mixed, or abstain
- only then produce final claim-evidence pairs
- run a narrow atomicity repair pass over final claim-evidence pairs
- keep the final review output simple: claim text, evidence item text, links, and provenance

This separates two decisions that are often conflated:

- what proposition is the paper asserting?
- what observation, statistic, result, or datum supports or evaluates that proposition?

## High-Level Architecture

```text
Input paper
  -> ingest as PDF / PDF URL / TEI XML / artifact.json
  -> build section inventory
  -> summarize each section
  -> summarize the paper from section summaries
  -> plan which sections are worth extracting
  -> Stage 1: extract raw candidate spans from each eligible section
  -> Stage 2: classify candidates, split compound spans into decomposed units, normalize claims/evidence, and link them
  -> Stage 3: repair compound final claims into atomic claim-evidence pairs
  -> materialize claims / evidence items / claim-evidence links
  -> gate out incomplete or unlinked local claim-evidence objects
  -> write JSON + CSV outputs
```

```mermaid
flowchart TD
    A[Input paper] --> B{Input type}
    B -->|PDF path| C[PDF ingest]
    B -->|PDF URL| D[Download PDF]
    B -->|TEI XML| E[TEI ingest]
    B -->|artifact.json| F[Load artifact]
    D --> C
    C --> G[Build spans and paper metadata]
    E --> G
    F --> G
    G --> H[Build section inventory]
    H --> I[Summarize sections]
    I --> J[Summarize paper]
    J --> K[Plan extractable sections]
    K --> L[Stage 1: candidate span extraction]
    L --> M[Stage 2: classify candidates]
    M --> N[Decompose compound spans into atomic units]
    N --> O[Normalize claims, evidence items, links]
    O --> P[Stage 3: atomicity repair]
    P --> Q[Materialize claims, evidence items, links]
    Q --> U[Gate incomplete or unlinked records]
    U --> R[Write section_context_v1_output.json]
    U --> S[Write extracted_claims.csv]
    U --> T[Write manifest.json]
```

## Main Code Path

The main runner is:

- `miner/v0/runner.py`

The extraction pipeline is composed of these stages:

1. `section_inventory.py`
   Builds section-level records from parsed spans.
2. `section_summary.py`
   Produces one structured summary per section.
3. `paper_summary.py`
   Produces one structured whole-paper summary from section summaries.
4. `section_gating.py`
   Decides which sections are worth extracting.
5. `section_claim_extractor.py`
   Runs candidate-span extraction, claim/evidence classification and linking, then atomicity repair.
6. `section_gating.py`
   Applies structural gating so unlinked claims/evidence are not exported.
7. `export.py`
   Writes final review-compatible artifacts.

## Staged Extraction Architecture

The actual v0 extraction happens in:

- `miner/v0/section_claim_extractor.py`

It runs two LLM calls per eligible section.

### Stage 1: Candidate Span Extraction

Prompt:

- `miner/v0/prompts/section_candidate_extraction_instructions.md`

The model receives:

- `paper_title`
- `paper_summary_json`
- `section_summary_json`
- `section_name`
- `section_type`
- `section_text`

It returns strict JSON:

```json
{
  "candidate_spans": [
    {
      "candidate_id": "c0",
      "source_text": "...",
      "initial_role_hint": "mixed",
      "reason": "..."
    }
  ]
}
```

Candidate spans are raw extraction units. They may be claims, evidence, background assumptions, method/result narration, or mixed spans that need splitting.

Stage 1 intentionally does not produce final claims.

### Stage 2: Classification, Splitting, and Linking

Prompt:

- `miner/v0/prompts/section_claim_extraction_instructions.md`

The model receives the same section context plus:

- `candidate_spans_json`
- `validation_feedback_json`

It returns strict JSON:

```json
{
  "classified_spans": [],
  "decomposed_units": [],
  "claims": [],
  "evidence_items": [],
  "claim_evidence_links": []
}
```

Stage 2 does five things:

1. Classifies each candidate span.
2. Splits mixed or compound spans when needed.
3. Emits split or unsplit atomic records as `decomposed_units`.
4. Normalizes claim-labelled decomposed units into final `claim_text`.
5. Normalizes evidence-labelled decomposed units into final `summary_text`.
6. Links each claim to direct evidence items.

### Stage 3: Atomicity Repair

Prompt:

- `miner/v0/prompts/section_atomicity_repair_instructions.md`

The repair stage receives:

- raw section text
- candidate spans
- classified spans
- decomposed units
- current claims
- current evidence items
- current claim-evidence links

It returns strict JSON:

```json
{
  "repair_actions": [],
  "claims": [],
  "evidence_items": [],
  "claim_evidence_links": []
}
```

This stage has a narrow job: detect compound final claims and return a complete repaired claim/evidence/link set for the section. If no repair is needed, it returns the original objects unchanged with a repair action explaining that no compound claims were found.

The stage is intentionally after normal extraction because compound claims are easiest to identify once final `claim_text`, evidence items, and links already exist.

```mermaid
flowchart TD
    A[Raw section text] --> B[Stage 1 candidate span extraction]
    B --> C[candidate_spans]
    C --> D[Stage 2 span classification]
    D --> E{primary_label}
    E -->|claim| F[Create claim-labelled decomposed unit]
    E -->|evidence| G[Create evidence-labelled decomposed unit]
    E -->|mixed| H[Split into smaller units]
    E -->|background_assumption| I[Keep as classified debug span]
    E -->|method_result| J[Keep as classified debug span or evidence context]
    E -->|abstain| K[Drop from final output]
    H --> U[decomposed_units]
    F --> U
    G --> U
    U --> L[Normalize claim-labelled units into final claims]
    U --> M[Normalize evidence-labelled units into final evidence_items]
    L --> N[Claim-evidence linking]
    M --> N
    N --> O[claim_evidence_links]
    I --> P[raw_section_outputs]
    J --> P
    K --> P
    C --> P
    D --> P
```

## Classification Labels

`classified_spans[*].primary_label` uses:

| Label | Meaning |
| --- | --- |
| `claim` | A checkable proposition asserted by the paper. |
| `evidence` | An observation, measurement, statistic, figure/table output, estimate, or reported datum used to evaluate a claim. |
| `background_assumption` | Prior knowledge, definitions, literature framing, or auxiliary assumptions. |
| `method_result` | What the paper did, used, measured, constructed, or immediately observed without a broader inferential claim. |
| `mixed` | A span that bundles claim/evidence/background/method material and should be split. |
| `abstain` | A span that should not be converted into final v0 review output. |

Additional internal labels include:

- `rhetorical_role`
- `claim_subtype`
- `evidence_type`
- `modality`
- `polarity`
- `attribution`
- `confidence`

These are stored for debugging and validation, but the reviewer-facing output remains claim/evidence focused.

## Claim/Evidence Policy

The v0 rule is:

```text
Claim = what is being asserted.
Evidence = the information used to evaluate that assertion.
```

Examples:

```text
Source: X was associated with Y, suggesting X contributes to disease risk.
Claim: X contributes to disease risk.
Evidence: X was associated with Y.
```

```text
Source: Variant A and Variant B were associated with trait Y, with P values p1 and p2, respectively.
Claim: Variant A is associated with trait Y with P value p1.
Evidence: The section reports Variant A among the associations for trait Y, with P value p1.
Claim: Variant B is associated with trait Y with P value p2.
Evidence: The section reports Variant B among the associations for trait Y, with P value p2.
```

The model should not emit one bundled claim when the source contains multiple separable findings. It should also avoid making `evidence_items[*].summary_text` a polished duplicate of `claim_text`; evidence should preserve the source-side result, statistic, or observation.

`decomposed_units` make atomization explicit. If a candidate says "two loci", "A and B", "respectively", or contains multiple identifiers with distinct statistics, the model should emit one claim-labelled decomposed unit per separable item before final claim normalization.

The atomicity repair stage repeats this check on final `claim_text`. This catches cases where the main extractor correctly identified an important candidate but still emitted a bundled final claim.

```mermaid
flowchart TD
    A[Candidate span] --> B{Does it mix reporting and interpretation?}
    B -->|Yes| C[Split into smaller clauses]
    C --> A
    B -->|No| D{Is it mainly self-referential or procedural?}
    D -->|Yes| E[method_result]
    D -->|No| F{Is it a concrete observation, statistic, result, figure/table output, or datum?}
    F -->|Yes| G[evidence]
    F -->|No| H{Does it assert a checkable effect, relation, mechanism, comparison, tendency, hypothesis, or conclusion?}
    H -->|Yes| I[claim]
    H -->|No| J{Is it prior knowledge, definition, literature framing, or auxiliary premise?}
    J -->|Yes| K[background_assumption]
    J -->|No| L[abstain]
```

## Runtime Inputs

At the pipeline level, `miner.v0` supports:

- PDF path
- downloadable PDF URL
- TEI XML
- `artifact.json`

At the extraction stage, the LLM receives:

- `paper_title`
- `paper_summary_json`
- `section_summary_json`
- `section_name`
- `section_type`
- `section_text`
- `candidate_spans_json` for Stage 2
- `validation_feedback_json` for Stage 2
For Stage 3, the LLM also receives the current claims, evidence items, links, and intermediate candidate/classification/decomposition records.

Summaries are orientation only. Claims and evidence must be grounded in raw section text.

## Raw Output Contract

### `candidate_spans[*]`

Expected fields:

- `candidate_id`
- `source_text`
- `initial_role_hint`
- `reason`

### `classified_spans[*]`

Expected fields:

- `candidate_id`
- `source_text`
- `primary_label`
- `rhetorical_role`
- `claim_subtype`
- `evidence_type`
- `modality`
- `polarity`
- `attribution`
- `confidence`

### `decomposed_units[*]`

Expected fields:

- `unit_id`
- `source_candidate_ids`
- `unit_text`
- `primary_label`
- `rhetorical_role`
- `claim_subtype`
- `evidence_type`
- `modality`
- `polarity`
- `attribution`
- `confidence`

### `claims[*]`

Expected fields:

- `claim_text`
- `source_candidate_ids`
- `claim_subtype`
- `modality`
- `polarity`
- `attribution`
- `extractor_confidence`

Compatibility fields such as `subject`, `predicate`, and `object` may be present, but they are not required for v0 review output.

### `evidence_items[*]`

Expected fields:

- `role`
- `summary_text`
- `source_candidate_ids`
- `evidence_type`
- `rhetorical_role`
- `evidence_method`
- `outcome_type`
- `presentation_type`
- `extractor_confidence`

### `claim_evidence_links[*]`

Expected fields:

- `claim_index`
- `evidence_index`
- `relation`
- `confidence`

### `repair_actions[*]`

Expected fields:

- `action`
- `reason`
- `source_claim_index`, when applicable
- `new_claim_indexes`, when applicable

## Runtime Outputs

Each run writes a paper output directory containing:

- `artifact.json`
- `section_context_v1_output.json`
- `extracted_claims.csv`
- `manifest.json`
- `tei.xml`, when generated from GROBID

The main JSON output contains:

- `paper`
- `sections`
- `section_summaries`
- `paper_summary`
- `section_extraction_plan`
- `claims`
- `evidence_items`
- `claim_evidence_links`
- `raw_section_outputs`

`raw_section_outputs` contains candidate spans, classified spans, decomposed units, atomicity repair actions, and pre-repair objects for debugging. It is useful for diagnosing whether a missed final claim was dropped during candidate extraction, span classification, decomposition, atomicity repair, linking, or structural gating.

## Reviewer-Facing Output

`extracted_claims.csv` remains the primary import file for ClaimsReview.

It contains:

- paper and section metadata
- `claim_text`
- linked evidence IDs
- evidence summary
- serialized evidence items
- serialized claim-evidence links

Internal span classification fields are not shown as first-class CSV columns.

## Design Notes

The v0 staged design follows a conservative extraction policy:

- label spans before generating final claims
- split mixed spans instead of forcing them into one record
- repair compound final claims after initial linking
- keep method/result and background material out of final claims unless the paper uses them as part of its own contribution
- preserve modality, polarity, attribution, and evidence type internally
- export a simple claim-evidence schema for review and validator scoring
