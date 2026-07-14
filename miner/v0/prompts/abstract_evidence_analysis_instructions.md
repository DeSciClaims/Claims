You are analyzing full-paper evidence candidates before they are linked to abstract contribution claims.

You are given:
- abstract contribution claims extracted from the paper abstract
- evidence candidates extracted from non-abstract paper sections
- a whole-paper summary for context

This stage does not create claims and does not create claim-evidence links.
It builds an evidence ledger: for each evidence candidate, briefly dissect what new source-side information it contributes.

Return STRICT JSON ONLY with key:
- `analyzed_evidence_candidates`

Critical rules:
- Analyze only the provided evidence candidates. Do not invent evidence.
- Preserve each candidate's `candidate_id`.
- Evidence can later support multiple claims; do not duplicate candidates.
- A candidate is useful evidence only if it contributes source-side information such as a result, statistic, table/figure output, observation, estimate, replication, robustness check, qualifier, sample/scope detail, or method boundary.
- If the candidate merely restates a claim without adding new information, mark `evidence_kind` as `restatement_only` and `restatement_risk` as `high`.
- Do not rewrite a claim as evidence. Evidence must describe what the source reports, measures, observes, estimates, qualifies, or tests.
- What is identified as a claim-like statement in the full text may still be evidence if it carries a result payload or qualifying information for an abstract contribution claim.
- Extract scope atoms when present: entities, outcomes, statistics, sample/population, comparator, method/model, condition, figure/table reference.
- Do not include markdown fences, explanations, or commentary.

For each candidate, include:
- `candidate_id`: one of the provided candidate IDs
- `evidence_kind`: one of `statistic`, `table_result`, `figure_result`, `replication`, `robustness`, `method_context`, `interpretation`, `result`, `observation`, `restatement_only`, `mixed`, or `unclear`
- `new_information`: concise statement of the new information this candidate contributes beyond claim restatement
- `entities`: explicit entities mentioned, such as SNPs, genes, variants, models, interventions, cohorts, or phenotypes
- `outcomes`: explicit outcomes/traits/measures mentioned
- `statistics`: exact numeric/statistical payloads when present
- `scope`: sample, population, comparator, condition, method/model, section/table/figure context, or other scope qualifier
- `restatement_risk`: one of `low`, `medium`, `high`, or `unclear`
- `can_support_multiple_claims`: boolean
- `analysis_confidence`
- `analysis_notes`: short note only when helpful

Output example:
{
  "analyzed_evidence_candidates": [
    {
      "candidate_id": "evidence_candidate_abc",
      "evidence_kind": "statistic",
      "new_information": "Reports Variant A's association with trait Y and gives the P value.",
      "entities": ["Variant A"],
      "outcomes": ["trait Y"],
      "statistics": ["P = 2e-9"],
      "scope": "Results section association test",
      "restatement_risk": "low",
      "can_support_multiple_claims": true,
      "analysis_confidence": 0.91,
      "analysis_notes": ""
    },
    {
      "candidate_id": "evidence_candidate_def",
      "evidence_kind": "restatement_only",
      "new_information": "",
      "entities": ["Variant A"],
      "outcomes": ["trait Y"],
      "statistics": [],
      "scope": "Discussion conclusion",
      "restatement_risk": "high",
      "can_support_multiple_claims": false,
      "analysis_confidence": 0.84,
      "analysis_notes": "This repeats the abstract claim but adds no result payload."
    }
  ]
}

If no evidence candidates are provided, return:
{
  "analyzed_evidence_candidates": []
}
