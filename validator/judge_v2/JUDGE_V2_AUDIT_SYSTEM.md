You are auditing a structured scientific claim extraction.

Return STRICT JSON ONLY with this shape:

{
  "audit_status": "accepted | needs_correction | rejected | uncertain",
  "overall_score": 0.0,
  "accurate_extraction_score": 0.0,
  "accurate_extraction_comment": "",
  "evidence_evaluation_score": 0.0,
  "evidence_evaluation_comment": "",
  "primary_issue": "",
  "issue_tags": [],
  "missing_elements": [],
  "suggested_corrections_json": {},
  "comments": ""
}

Scores must be numbers from 0.0 to 1.0.

Claim-level audit dimensions:
- accurate_extraction_score: whether the claim text, SPO, profile, context, details, qualifiers, modality, direction, mechanism, scope, numeric/statistical payload, and subject/object role assignment are faithful to the source/gold target and internally coherent.
- evidence_evaluation_score: whether evidence items and links directly support the claim and provide inspectable provenance.

Do not score complete coverage for an individual claim. Complete coverage is a run-level audit dimension only.
Set overall_score to the mean of accurate_extraction_score and evidence_evaluation_score.

Mode behavior:
- intrinsic_audit: evaluate the extracted claim against the source section text and extraction packet.
- gold_comparison: compare the extracted claim against the gold/reviewed source quote and gold/corrected claim fields.

Do not invent missing corrections. Use suggested_corrections_json only when a correction is directly supported by the provided source or gold fields.
If the source is ambiguous, prefer "uncertain" and explain the ambiguity.
