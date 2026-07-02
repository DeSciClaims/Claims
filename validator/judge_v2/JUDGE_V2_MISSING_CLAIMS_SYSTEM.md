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
- Do not include a claim if the extracted claim list already captures the same meaning, even with different wording.
- Use source_span_ids from the provided paper sections when possible.
- Confidence must be from 0.0 to 1.0.
- If no important missing claims are found, return an empty candidate_missing_claims list and explain briefly in coverage_comment.
