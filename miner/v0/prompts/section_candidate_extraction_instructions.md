You are extracting raw candidate spans from the ORIGINAL RAW TEXT of one section of a scientific paper.

This is stage 1 of a multi-stage extraction pipeline. Do not write final claims yet.

Return STRICT JSON ONLY with key:
- `candidate_spans`

Each candidate span should be a clause, sentence, or short group of adjacent clauses that may later be classified as one of:
- claim
- evidence
- background_assumption
- method_result
- mixed

Candidate selection rules:
- Extract spans from the raw section text only.
- Prefer candidate spans that contain one or more of:
  - a proposition asserted by this paper
  - a concrete observation, statistic, result, figure/table output, estimate, benchmark, or reported datum
  - a mixed sentence where measured results and interpretation are bundled together
  - a method/result statement that may be needed as evidence context
- Include compound sentences that contain multiple variants, outcomes, samples, models, timepoints, or conditions; do not pre-collapse them.
- When a sentence lists multiple identifiers/entities with separate statistics, emit separate candidate spans for each item when the item-level text can be made clear from the sentence.
- For "respectively" constructions, pair each identifier/entity with its corresponding statistic and emit one candidate per pair.
- You may also include the original compound sentence as a `mixed` candidate when useful, but item-level candidates are required for item-level findings.
- Include local evidence-bearing results even when they are not final claims.
- Include enough surrounding words to preserve meaning-critical qualifiers, but avoid whole paragraphs.
- Do not extract from whole-paper or section summaries.
- Do not include markdown fences, explanations, or commentary.

For each candidate span, return:
- `candidate_id`: short stable ID within this section, such as `c0`, `c1`, `c2`
- `source_text`: exact or near-exact text from the section
- `initial_role_hint`: one of `claim`, `evidence`, `background_assumption`, `method_result`, `mixed`, or `unclear`
- `reason`: short explanation of why this span may matter

Output example:
{
  "candidate_spans": [
    {
      "candidate_id": "c0",
      "source_text": "Variant A and Variant B were associated with trait Y, with P values p1 and p2, respectively.",
      "initial_role_hint": "mixed",
      "reason": "Reports multiple evidence-bearing association results that may need to be split."
    },
    {
      "candidate_id": "c1",
      "source_text": "Variant A was associated with trait Y with P value p1.",
      "initial_role_hint": "claim",
      "reason": "Item-level finding from a compound result sentence."
    },
    {
      "candidate_id": "c2",
      "source_text": "Variant B was associated with trait Y with P value p2.",
      "initial_role_hint": "claim",
      "reason": "Item-level finding from a compound result sentence."
    }
  ]
}

If the section contains no plausible candidate spans, return:
{
  "candidate_spans": []
}
