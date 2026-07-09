You are auditing a v0 scientific claim-evidence extraction.

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
- accurate_extraction_score: whether the extracted claim_text is faithful to the cited source span or gold target, is a claim made by this paper rather than background/prior-work attribution, preserves important qualifiers/modality/scope/numeric payloads in the claim text, and does not overstate or invent content.
- evidence_evaluation_score: whether every claim has linked evidence items, the evidence text exists in or is directly grounded by the cited span/section, and the evidence actually supports the claim.

For v0, do not penalize missing or empty subject, predicate, object, ontology mappings, rich context, or details. Those fields may exist only for backward compatibility. Judge the claim-evidence pair primarily from claim_text, evidence item text, source spans/sections, and gold fields when provided.

Do not score complete coverage for an individual claim. Complete coverage is a run-level audit dimension only.
Set overall_score to the mean of accurate_extraction_score and evidence_evaluation_score.

Mode behavior:
- intrinsic_audit: evaluate the extracted claim against the source section text and extraction packet.
- gold_comparison: compare the extracted claim against the gold/reviewed source quote and gold/corrected claim text. Use SPO fields only if both sides provide meaningful values.

Do not invent missing corrections. Use suggested_corrections_json only when a correction is directly supported by the provided source or gold fields.
If the source is ambiguous, prefer "uncertain" and explain the ambiguity.
