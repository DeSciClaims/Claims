# Judge V1 Fields

## Input Fields

Judge V1 reads a `section_context_v1_output.json` file and builds one evaluation row per claim.

Core input fields:

| Field | Description |
| --- | --- |
| `paper` | Paper metadata, including `paper_id`, title, DOI, journal, and year when available. |
| `sections` | Source section records and raw section text. |
| `section_summaries` | Section-level summaries used as paper context. |
| `paper_summary` | Paper-level summary used as paper context. |
| `claims` | Extracted claim objects to judge. |
| `evidence_items` | Extracted evidence objects. |
| `claim_evidence_links` | Links between claims and evidence items. |

Per-claim input fields:

| Field | Description |
| --- | --- |
| `claim_id` | Unique extracted claim ID. |
| `paper_id` | Paper ID for the claim. |
| `claim_profile` | Claim schema/profile. |
| `claim_text` | Extracted natural-language claim. |
| `subject` | Claim SPO subject. |
| `predicate` | Claim SPO predicate. |
| `object` | Claim SPO object. |
| `context` | Claim-level qualifiers. |
| `details` | Structured claim payload. |
| `source_span_ids` | Source span IDs grounding the claim. |

Gold mode also reads:

| Field | Description |
| --- | --- |
| `gold-reviewed-file` | Reviewed CSV/XLSX file passed with `--gold-reviewed-file`. |
| `review_source_quote` | Human-reviewed source quote from the gold file. |
| `review_section_name` | Human-reviewed section name from the gold file. |

## Output Fields

Judge V1 writes either:

- `section_context_v1_intrinsic_judgment.csv`
- `section_context_v1_gold_evaluation.csv`

Base output fields:

| Field | Description |
| --- | --- |
| `paper_id` | Paper ID. |
| `section_id` | Matched section/span ID. |
| `section_name` | Matched section name. |
| `section_text` | Raw section text, intrinsic mode only. |
| `claim_id` | Evaluated claim ID. |
| `claim_profile` | Evaluated claim profile. |
| `claim_text` | Evaluated claim text. |
| `subject` | Evaluated SPO subject. |
| `predicate` | Evaluated SPO predicate. |
| `object` | Evaluated SPO object. |
| `context_summary` | Compact context summary. |
| `context_json` | Full context JSON. |
| `details_summary` | Compact details summary. |
| `details_json` | Full details JSON. |
| `linked_evidence_ids` | Linked evidence IDs. |
| `evidence_summary` | Compact evidence summary. |
| `evidence_items_json` | Full evidence item JSON. |
| `claim_evidence_links_json` | Full claim-evidence link JSON. |

Gold-only output fields:

| Field | Description |
| --- | --- |
| `review_group_id` | Reviewed quote group ID. |
| `review_section_name` | Human-reviewed section name. |
| `review_source_quote` | Human-reviewed source quote. |
| `match_score` | Match score between reviewed quote group and extracted claim. |

LLM judge output fields:

| Field | Description |
| --- | --- |
| `llm_judge_v1_decision` | `accept`, `revise`, `reject`, or `parse_error`. |
| `llm_judge_v1_overall_score` | Final normalized score from `0.0` to `1.0`. |
| `llm_judge_v1_primary_failure` | Main failure reason. |
| `llm_judge_v1_secondary_failures` | Additional failure reasons. |
| `llm_judge_v1_error_tags` | Error tags. |
| `llm_judge_v1_missing_elements` | Missing pieces identified by the judge. |
| `llm_judge_v1_feedback` | Short judge feedback. |

Diagnostic output fields:

| Field | Description |
| --- | --- |
| `llm_judge_v1_is_meaningful_claim` | Whether the extracted row is a meaningful scientific claim/result. |
| `llm_judge_v1_claim_text_faithful` | Whether the claim text faithfully represents the paper. |
| `llm_judge_v1_claim_is_self_consistent` | Whether the claim object is internally coherent. |
| `llm_judge_v1_local_context_sufficient` | Whether local claim text/context/details are enough to read the claim safely. |
| `llm_judge_v1_context_supported_elsewhere_in_paper` | Whether missing context is recoverable elsewhere in the extraction packet. |
| `llm_judge_v1_supporting_evidence_present_somewhere` | Whether supporting evidence exists locally or elsewhere in the extraction packet. |
| `llm_judge_v1_evidence_links_complete` | Whether evidence links are complete enough for provenance inspection. |
| `llm_judge_v1_spo_graph_compatible` | Whether the SPO projection is graph-friendly and consistent with the full claim. |

Dimension output fields:

| Field | Description |
| --- | --- |
| `llm_judge_v1_claim_target_selection` | Score for whether the extraction targets a substantive paper-specific claim/result. |
| `llm_judge_v1_claim_target_selection_reason` | Reason for `claim_target_selection`. |
| `llm_judge_v1_claim_faithfulness` | Score for whether the claim is faithful and non-misleading. |
| `llm_judge_v1_claim_faithfulness_reason` | Reason for `claim_faithfulness`. |
| `llm_judge_v1_local_context_capture` | Score for whether local qualifiers/conditions/details are captured. |
| `llm_judge_v1_local_context_capture_reason` | Reason for `local_context_capture`. |
| `llm_judge_v1_paper_context_alignment` | Score for whether paper-level context supports the claim interpretation. |
| `llm_judge_v1_paper_context_alignment_reason` | Reason for `paper_context_alignment`. |
| `llm_judge_v1_details_quality` | Score for whether structured details are complete and sensibly placed. |
| `llm_judge_v1_details_quality_reason` | Reason for `details_quality`. |
| `llm_judge_v1_spo_graph_quality` | Score for whether the SPO is atomic, queryable, and consistent with the claim. |
| `llm_judge_v1_spo_graph_quality_reason` | Reason for `spo_graph_quality`. |
| `llm_judge_v1_evidence_support_presence` | Score for whether relevant supporting evidence is present. |
| `llm_judge_v1_evidence_support_presence_reason` | Reason for `evidence_support_presence`. |
| `llm_judge_v1_evidence_linking_completeness` | Score for whether evidence links preserve provenance clearly enough. |
| `llm_judge_v1_evidence_linking_completeness_reason` | Reason for `evidence_linking_completeness`. |

When `--judge-version none` is used, the base output fields are written without `llm_judge_v1_*` fields.
