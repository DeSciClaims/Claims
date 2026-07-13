You are linking abstract claims to evidence candidates from the full text of the same paper.

You are given:
- abstract claims extracted from the paper abstract
- evidence candidates extracted from non-abstract paper sections
- a whole-paper summary for context

The abstract claims define the claim universe. Do not invent new claims.
The evidence candidates define the allowed evidence universe. Do not invent evidence outside these candidates.
If `validation_feedback_json` is non-empty, it identifies evidence-linking problems in a previous output. Fix those exact problems and return the full revised `evidence_items` and `claim_evidence_links` lists.

Return STRICT JSON ONLY with keys:
- `evidence_items`
- `claim_evidence_links`

Critical rules:
- Link every relevant evidence candidate that directly supports, qualifies, weakens, contradicts, or fails to support an abstract claim.
- Evidence must be grounded in one or more provided evidence candidates.
- Prefer direct results, measurements, statistics, tables, figures, estimates, observations, experiment outputs, robustness checks, replication results, or model-performance results.
- Do not use background, prior-work literature, motivation, or generic methods as support unless it directly evaluates the abstract claim.
- Do not use a candidate as evidence merely because it repeats the claim. Evidence should contain the source-side observation, statistic, result, datum, figure/table output, or method/result detail that evaluates the claim.
- Do not link a claim to a generic methods statement when the claim is about a biological interpretation, effect size, phenotype category, replication result, or power-analysis use.
- A claim about effect size must be linked to evidence containing the relevant effect-size payload.
- A claim about cognitive function must be linked to evidence that directly mentions cognitive function.
- A claim about anterior caudate nucleus involvement must be linked to evidence that directly mentions anterior caudate, caudate cells, or the relevant bioinformatics enrichment.
- A claim about health, cognitive, or central nervous system phenotypes must be linked to evidence that directly mentions those phenotype categories.
- A claim about power analyses must be linked to evidence that directly mentions power analyses, benchmark effects, or the estimate used for power calculations.
- A single abstract claim may need multiple evidence items.
- A single evidence item may support multiple abstract claims.
- If no provided candidate is relevant to a claim, leave that claim unlinked rather than forcing a weak link.
- Preserve exact numeric/statistical payloads, sample names, model names, thresholds, timepoints, comparators, and figure/table references when they are support.
- Do not include markdown fences, explanations, or commentary.

For each evidence item, include:
- `summary_text`: concise evidence text grounded in provided candidate text
- `source_candidate_ids`: one or more candidate IDs from `evidence_candidates_json`
- `role`: usually `supports`, but use `qualifies`, `contradicts`, or `weakens` when appropriate
- `evidence_type`: one of `statistic`, `figure`, `table`, `observation`, `estimate`, `dataset`, `methodological_detail`, `text`, `none`, or `unclear`
- `rhetorical_role`: one of `method`, `experiment`, `observation`, `result`, `conclusion`, `model`, or `unclear`
- `evidence_method`: usually `textual_evidence`
- `presentation_type`: usually `text`, unless the candidate clearly refers to a `table` or `figure`
- `extractor_confidence`

For each link, include:
- `claim_index`: zero-based index into the provided abstract claims
- `evidence_index`: zero-based index into your returned evidence_items
- `relation`: one of `supports`, `qualifies`, `contradicts`, `weakens`, or `not_supported`
- `confidence`

Output example:
{
  "evidence_items": [
    {
      "summary_text": "The Results section reports that Variant A was associated with trait Y with P = 2e-9.",
      "source_candidate_ids": ["evidence_candidate_abc"],
      "role": "supports",
      "evidence_type": "statistic",
      "rhetorical_role": "result",
      "evidence_method": "textual_evidence",
      "presentation_type": "text",
      "extractor_confidence": 0.89
    }
  ],
  "claim_evidence_links": [
    {
      "claim_index": 0,
      "evidence_index": 0,
      "relation": "supports",
      "confidence": 0.88
    }
  ]
}

If no evidence candidates support any abstract claim, return:
{
  "evidence_items": [],
  "claim_evidence_links": []
}
