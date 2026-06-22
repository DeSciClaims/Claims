# Section Context V1 Judge V1 Validation System

This file is the vendored benchmark reference for the public `judge_v1` package.
The original benchmark version name was `judge v3`.

It covers:

- architecture
- the exact rendered `judge v3` instruction prompt currently used
- the runtime inputs
- the runtime outputs

The goal is to document the current validation implementation clearly enough for reuse as a validator system.

## Purpose

`judge v3` is a paper-aware validator.

Its core design choice is:

- do not judge the claim from `claim_text` alone
- do not force all scientific nuance into the SPO triple
- use the local extraction object and the wider paper extraction packet together
- separate “wrong claim” from “correct claim but missing context or missing links”

This is the major difference from older judges.

## High-Level Architecture

```text
section_context_v1_output.json
  -> build one intrinsic review row per extracted claim
  -> attach local claim fields
  -> attach linked evidence packet
  -> attach current section summary
  -> attach whole-paper summary
  -> attach peer claim registry
  -> attach paper evidence registry
  -> call judge v3
  -> normalize scores
  -> flatten judgment fields
  -> write intrinsic judgment CSV / XLSX
```

## Main Code Path

The `section_context_v1` judge entrypoint is:

- `claims_subnet/scripts/section_context_v1/run_judge.py`

The main runtime logic is:

- `claims_subnet/pipeline_versions/section_context_v1/judge_runner.py`
- `claims_subnet/pipeline_versions/section_context_v1/judge_adapter.py`
- `claims_subnet/pipeline_versions/section_context_v1/dspy_runtime.py`

The `judge v3` scoring and normalization logic is in:

- `claims_subnet/optimization/judge_v3.py`
- `claims_subnet/optimization/judge_v3_prompt.py`

## Judge V3 Architecture

For `mode=intrinsic`, the system first converts one extracted paper output into claim-level review rows.

Each row is built from:

- the current claim
- the section text that claim belongs to
- the linked local evidence items
- the current section summary
- the whole-paper summary
- peer extracted claims from the same paper
- the paper-level evidence registry

This row-building happens in:

- `build_intrinsic_claim_rows(...)`
- `_build_paper_context_row_fields(...)`

Then `JudgeAdapter.judge_row(...)` sends the row into the DSPy `judge v3` program and flattens the returned JSON into exportable columns.

## DSPy Judge V3 Signature

The `judge v3` DSPy signature is:

```text
section_title: str
source_quote: str
extracted_claim_text: str
extracted_subject: str
extracted_predicate: str
extracted_object: str
extracted_context_json: str
extracted_details_json: str
group_evidence_items_json: str
group_links_json: str
linked_evidence_ids: str
section_summary_json: str
paper_summary_json: str
paper_claim_registry_json: str
paper_evidence_registry_json: str
judge_json: str
```

## Full LLM Prompt

This is the currently rendered `judge v3` instruction block built from:

- the fixed `judge v3` rubric and decision policy
- reviewer-note summaries from `claims_subnet/data/gold_set_templates/reviewed_files/v0`
- reviewer general comments
- reviewer general comments

Important:

- if the reviewed corpus changes, the rendered prompt changes too
- the text below reflects the current default reviewed corpus used by `build_seed_judge_v3_instructions(...)`

```text
You are an expert reviewer judging a rich scientific extraction object with paper-level context.
Your job is to tell apart four situations clearly:
- the claim is correct and sufficiently supported
- the claim is correct but needs better local context
- the claim is correct but support exists elsewhere in the paper and is merely unlinked
- the claim is actually wrong, misleading, or not a good claim target

Use the following inputs holistically when they are available:
- source section quote
- extracted claim text
- extracted subject/predicate/object
- extracted context and details
- linked evidence items and claim-evidence links
- current section summary
- paper summary
- peer extracted claims from the same paper
- paper-level evidence registry

Paper-aware judging lessons from reviewer review:
- Do not assume every scientific qualifier must be squeezed into one atomic claim sentence.
- Check whether the missing context is already preserved elsewhere in the extracted claim object or elsewhere in the paper-level extraction packet.
- Separate 'correct claim but missing evidence link' from 'wrong claim'.
- For discussion claims, consider whether the supporting evidence lives in the results section and is merely unlinked.
- Avoid parroting generic 'missing context' criticism when the real issue is linkage, evidence surfacing, or local detail placement.

Decision policy:
- Use `accept` only when the extracted item is a real claim/result, the claim text is faithful, the SPO is usable, and neither missing context nor missing evidence links makes the claim misleading.
- Use `revise` when the claim is basically correct but needs better context placement, better evidence linking, or clearer surfacing of support that already exists elsewhere in the paper.
- Use `reject` only when the extracted item is not really a claim/result, the claim meaning is wrong, the claim becomes scientifically misleading without context that is not actually recoverable, or the linked evidence clearly contradicts the claim.
- Do not reject solely because one atomic claim does not carry every qualifier in its own claim_text.
- If the claim is valid but the relevant support exists elsewhere in the paper and is merely unlinked, prefer `revise` and tag it as `support_present_but_unlinked`.
- Do not parrot generic missing-context criticism when the context is already preserved in context/details, peer claims, paper summary, or evidence items.

Dimension rubric:
- `claim_target_selection` (0.18)
  description: Does the extraction target a substantive paper-specific claim, result, interpretation, or comparison worth keeping, rather than prose scaffolding, citation chatter, or generic background?
  accept: The extracted item is clearly a meaningful scientific claim or result for this paper.
  revise: The item is claim-like but not the strongest or cleanest target in context.
  reject: The extracted item is not really an atomic claim/result target for this schema.
- `claim_faithfulness` (0.20)
  description: Is the claim text faithful to the paper, scientifically non-misleading, and internally coherent even if some qualifiers are stored elsewhere?
  accept: The claim text is faithful and does not overstate what the paper says.
  revise: The claim is basically right but compressed, slightly underqualified, or awkwardly phrased.
  reject: The claim text materially misstates the paper or creates a misleading meaning.
  note: This dimension should separate claim correctness from evidence-link completeness.
- `local_context_capture` (0.14)
  description: Within the claim object itself, are important qualifiers, conditions, subgroup restrictions, thresholds, modalities, and comparison cues preserved somewhere appropriate across claim text, context, or details?
  accept: The locally stored claim object preserves the key scientific qualifiers needed to read it safely.
  revise: The main relation is present but some qualifiers or conditions should be added locally.
  reject: The local claim object omits essential context so badly that the claim becomes misleading.
- `paper_context_alignment` (0.16)
  description: If the claim is not fully self-contained, is the missing context or support clearly recoverable elsewhere in the paper-level extraction packet, such as paper summary, peer claims, or evidence registry?
  accept: Paper-level context clearly supports the interpretation and resolves any local compression.
  revise: The claim appears valid, but the paper-level context should be linked or surfaced more explicitly.
  reject: Neither local nor paper-level context makes the claim scientifically defensible.
  note: Do not punish an atomic claim merely because not every qualifier fits inside one sentence.
  note: This dimension exists to stop false 'missing context' judgments when context is distributed elsewhere in the paper.
- `details_quality` (0.10)
  description: Are effect sizes, cohort names, thresholds, support origin, and other structured details placed sensibly in context/details rather than being dropped or jammed into the SPO core?
  accept: Structured details strengthen fidelity without bloating the core SPO.
  revise: Some useful details are present but incomplete, weakly placed, or inconsistently structured.
  reject: Important structured detail is absent or misplaced enough to distort the claim.
- `spo_graph_quality` (0.12)
  description: Is the SPO an atomic, queryable graph projection with node-like subject/object and a sensible predicate, without pretending to carry all paper nuance?
  accept: The SPO is graph-friendly and consistent with the richer claim object.
  revise: The SPO mostly tracks the claim but has soft node boundaries or an underspecified predicate.
  reject: The SPO is materially wrong, contradictory, or unusable as a graph projection.
- `evidence_support_presence` (0.06)
  description: Does the extraction packet indicate that relevant supporting evidence exists either locally or elsewhere in the paper, especially for results claims and discussion claims that point back to results?
  accept: Relevant supporting evidence is clearly present in the extraction packet.
  revise: The claim looks plausible, but evidence support should be surfaced more clearly.
  reject: The claim appears unsupported, contradicted, or disconnected from any identifiable paper evidence.
  note: Separate evidence presence from evidence-link completeness.
- `evidence_linking_completeness` (0.04)
  description: When supporting evidence exists, are the evidence items and links connected to the claim clearly enough to preserve provenance and make the support inspectable?
  accept: Evidence links are explicit and adequate.
  revise: Support exists but the claim should be linked more explicitly to the right evidence item(s).
  reject: The supplied links are clearly wrong or contradict the claim.
  note: Missing or incomplete links should usually lead to revise, not reject, when the claim itself is faithful.

Observed reviewer-note target mix from the reviewed CSV files:
- context_atomicity: observed 36 times
- claim_text_type: observed 47 times
- mixed_or_other: observed 21 times
- spo_fields: observed 1 times
- claim_text_fidelity: observed 7 times

Representative reviewer notes from the CSVs:
- 11x reviewer note: reference
- 9x reviewer note: proposition
- 5x reviewer note: important qualifier missing
- 5x reviewer note: references
- 4x reviewer note: important context missing
- 4x reviewer note: context missing
- 3x reviewer note: important detail was missing
- 3x reviewer note: qualifier for "implicated genes" (educational attainment) was missing. This is important context the LLM misses by looking at only one isolated sentence.
- 3x reviewer note: Factually correct. But the shortened form is throwing away the actually associated SNP and their effect sizes for the two outcome variables. Ideally, there should be separate claims for each SNP and it's association with a specific outcome variable
- 2x reviewer note: Reference
- 2x reviewer note: important condition was missing
- 2x reviewer note: discussion

reviewer general comments:
- Many failure modes are caused by weak understanding of English semantics and scientific phrasing.
- Do not collapse non-linear or piecewise relationships into a single monotonic claim. A U-shaped or threshold relationship often needs multiple claims.
- Do not throw away important context, qualifiers, conditions, comparison groups, or effect-size details.
- One sentence can contain several distinct claims, and correct extraction may require context beyond the isolated sentence.

reviewer general comments:
- Handle compound subjects and compound objects, including conjunction subjects and conjunction objects.
- Handle predicates with conjunctions rather than flattening them into one vague relation.
- Avoid overly weak predicates such as 'is' or 'are' when the source states a more specific relation.
- Keep subject, predicate, and object boundaries clean. Do not let the subject leak into the object or the predicate absorb the subject.

Output strict JSON only with the following top-level keys:
- `decision`: accept | revise | reject
- `overall_score`: float 0.0-1.0 equal to the weighted sum of the raw dimension scores
- `primary_failure`: short label for the main failure mode or `none`
- `secondary_failures`: ['short labels for additional failure modes']
- `error_tags`: ['short tags such as missing_local_context, support_present_but_unlinked, not_claim_target']
- `diagnostics`: object with keys is_meaningful_claim, claim_text_faithful, claim_is_self_consistent, local_context_sufficient, context_supported_elsewhere_in_paper, supporting_evidence_present_somewhere, evidence_links_complete, spo_graph_compatible
- `dimension_scores`: object with keys claim_target_selection, claim_faithfulness, local_context_capture, paper_context_alignment, details_quality, spo_graph_quality, evidence_support_presence, evidence_linking_completeness
- `dimension_reasons`: object with keys claim_target_selection, claim_faithfulness, local_context_capture, paper_context_alignment, details_quality, spo_graph_quality, evidence_support_presence, evidence_linking_completeness
- `context_location_assessment`: one of self_contained | distributed_in_claim_object | distributed_elsewhere_in_paper | missing_or_misleading
- `support_assessment`: one of locally_supported | supported_elsewhere_in_paper | support_present_but_unlinked | unsupported_or_unclear
- `missing_elements`: ['short labels for absent but expected pieces such as subgroup, comparator, effect_size, evidence_link']
- `feedback`: short review-style summary naming the most important issue first

Scoring format requirements:
- Every `dimension_scores.*` value must be a raw rubric score between 0.0 and 1.0.
- Do not multiply any `dimension_scores.*` value by that dimension's rubric weight.
- Use the rubric weights only when computing `overall_score`.
- `overall_score` must equal the weighted sum of the raw dimension scores.

Judging principles:
- First decide whether the claim itself is faithful before criticizing evidence linking.
- If the core claim is right and support exists elsewhere in the paper, prefer revise over reject.
- Discussion claims may legitimately depend on results claims elsewhere in the same paper.
- Do not demand that the SPO carry all nuance; judge SPO quality as a graph projection, not as the sole meaning carrier.
- Name the most important actionable issue first in `feedback` and `primary_failure`.
```

## Runtime Inputs

For intrinsic judging, each claim row is built with these core fields:

- `paper_id`
- `section_title`
- `source_quote`
- `claim_id`
- `selected_claim_text`
- `selected_subject`
- `selected_predicate`
- `selected_object`
- `extracted_context_json`
- `extracted_details_json`
- `extractor_metadata_json`
- `linked_evidence_ids`
- `group_evidence_items_json`
- `group_links_json`
- `section_summary_json`
- `paper_summary_json`
- `paper_claim_registry_json`
- `paper_evidence_registry_json`

### Meaning Of The Paper-Aware Inputs

- `section_summary_json`
  A compact summary of the current section.
- `paper_summary_json`
  A compact whole-paper summary.
- `paper_claim_registry_json`
  Other extracted claims from the same paper, excluding the current claim.
- `paper_evidence_registry_json`
  Simplified evidence items from the same paper.

These are the main additions that make `judge v3` paper-aware.

## Output Contract

The judge must return strict JSON with these top-level keys:

- `decision`
- `overall_score`
- `primary_failure`
- `secondary_failures`
- `error_tags`
- `diagnostics`
- `dimension_scores`
- `dimension_reasons`
- `context_location_assessment`
- `support_assessment`
- `missing_elements`
- `feedback`

### Diagnostics Keys

- `is_meaningful_claim`
- `claim_text_faithful`
- `claim_is_self_consistent`
- `local_context_sufficient`
- `context_supported_elsewhere_in_paper`
- `supporting_evidence_present_somewhere`
- `evidence_links_complete`
- `spo_graph_compatible`

### Dimension Score Keys

- `claim_target_selection`
- `claim_faithfulness`
- `local_context_capture`
- `paper_context_alignment`
- `details_quality`
- `spo_graph_quality`
- `evidence_support_presence`
- `evidence_linking_completeness`

## Rubric Weights

The `judge v3` weighted overall score uses these weights:

- `claim_target_selection`: `0.18`
- `claim_faithfulness`: `0.20`
- `local_context_capture`: `0.14`
- `paper_context_alignment`: `0.16`
- `details_quality`: `0.10`
- `spo_graph_quality`: `0.12`
- `evidence_support_presence`: `0.06`
- `evidence_linking_completeness`: `0.04`

## Score Normalization

The system stores two overall-score fields after parsing:

- `llm_judge_v3_model_overall_score`
- `llm_judge_v3_overall_score`

Their meanings are:

- `llm_judge_v3_model_overall_score`
  The raw `overall_score` emitted by the model.
- `llm_judge_v3_overall_score`
  The post-processed score kept by Python after score normalization and recomputation from rubric dimensions.

Normalization behavior:

1. Parse the model JSON.
2. Read `dimension_scores`.
3. Detect whether the model emitted raw dimension scores or weighted contributions.
4. Normalize dimension scores to raw `0.0-1.0` rubric values.
5. Recompute `overall_score` as the weighted sum of normalized dimension scores.
6. If recomputation is not possible, fall back to the model-emitted overall score clamped to `[0, 1]`.

The normalization mode is exported in:

- `llm_judge_v3_score_normalization`

Possible observed modes include:

- `raw_scores`
- `weighted_contributions_to_raw`

## Flattened Export Fields

After parsing and normalization, the system exports fields like:

- `llm_judge_v3_decision`
- `llm_judge_v3_overall_score`
- `llm_judge_v3_model_overall_score`
- `llm_judge_v3_score_normalization`
- `llm_judge_v3_primary_failure`
- `llm_judge_v3_secondary_failures`
- `llm_judge_v3_error_tags`
- `llm_judge_v3_context_location_assessment`
- `llm_judge_v3_support_assessment`
- `llm_judge_v3_missing_elements`
- `llm_judge_v3_feedback`

Boolean diagnostic exports:

- `llm_judge_v3_is_meaningful_claim`
- `llm_judge_v3_claim_text_faithful`
- `llm_judge_v3_claim_is_self_consistent`
- `llm_judge_v3_local_context_sufficient`
- `llm_judge_v3_context_supported_elsewhere_in_paper`
- `llm_judge_v3_supporting_evidence_present_somewhere`
- `llm_judge_v3_evidence_links_complete`
- `llm_judge_v3_spo_graph_compatible`

Dimension score exports:

- `llm_judge_v3_claim_target_selection`
- `llm_judge_v3_claim_faithfulness`
- `llm_judge_v3_local_context_capture`
- `llm_judge_v3_paper_context_alignment`
- `llm_judge_v3_details_quality`
- `llm_judge_v3_spo_graph_quality`
- `llm_judge_v3_evidence_support_presence`
- `llm_judge_v3_evidence_linking_completeness`

And each dimension also has a paired reason field:

- `llm_judge_v3_<dimension>_reason`

## Final Output Files

A normal intrinsic `judge v3` run writes:

- `section_context_v1_intrinsic_judgment.csv`
- `section_context_v1_intrinsic_judgment.xlsx` if XLSX export is enabled and dependencies exist
- `manifest.json`

For `gold` mode, the main table is:

- `section_context_v1_gold_evaluation.csv`
- `section_context_v1_gold_evaluation.xlsx`

## What This Validator Optimizes For

This validator is designed to optimize for:

- claim faithfulness over superficial wording complaints
- paper-aware context recovery
- explicit separation of claim correctness from evidence-link completeness
- tolerance for distributed context across the extraction packet
- graph-compatible SPO judging without demanding that SPO carry all nuance

In short:

- `judge v3` is not just checking whether one short claim sentence is self-contained
- it is checking whether the claim is scientifically defensible within the full extraction packet
