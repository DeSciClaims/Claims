You are auditing claim extraction coverage for a scientific paper.

Your task is to identify important claims present in the paper text that are missing from the miner's extracted claim list.

Return STRICT JSON ONLY with this shape:

{
  "candidate_missing_claims": [
    {
      "candidate_claim_text": "",
      "candidate_subject": "",
      "candidate_predicate": "",
      "candidate_object": "",
      "source_span_ids": [],
      "confidence": 0.0,
      "missing_reason": ""
    }
  ],
  "coverage_comment": ""
}

Rules:
- Only include a candidate missing claim if it is important to the paper's scientific findings, methods/results interpretation, or central conclusions.
- Do not include background statements, motivation, literature review claims, or generic context unless they are central claims made by this paper.
- A claim is a checkable proposition to be evaluated, not the supporting observation or statistic itself.
- Do not list standalone evidence, methods, or measurements as missing claims unless the paper presents them as focal asserted findings.
- Include hypotheses as tentative missing claims when they are made by this paper and are important to the argument.
- Prefer missing claims that are falsifiable or testable in principle, while preserving necessary model, method, scope, and assumption context.
- Do not include a claim if the extracted claim list already captures the same meaning, even with different wording.
- Use source_span_ids from the provided paper sections when possible.
- Put the full meaning in candidate_claim_text. The candidate_subject, candidate_predicate, and candidate_object fields are backward-compatible placeholders and may be empty.
- Confidence must be from 0.0 to 1.0.
- If no important missing claims are found, return an empty candidate_missing_claims list and explain briefly in coverage_comment.
