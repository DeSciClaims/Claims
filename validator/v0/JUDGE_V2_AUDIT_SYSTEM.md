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
- accurate_extraction_score: whether the extracted claim_text is faithful to the cited source span or gold target, is atomic, is a claim made by this paper rather than background/prior-work attribution, preserves important qualifiers/modality/scope/numeric payloads in the claim text, and does not overstate or invent content.
- evidence_evaluation_score: whether every claim has linked evidence items, the evidence text exists in or is directly grounded by the cited span/section, is distinct from the claim, and actually supports the claim.

Claim/evidence distinction:
- A scientific claim is a checkable proposition that asserts something about the world: an effect, relation, mechanism, comparison, tendency, hypothesis, or conclusion.
- Evidence is not the claim itself. It is the observation, measurement, statistic, experimental result, figure/table output, or reported datum used to support, weaken, contradict, qualify, or fail to support the claim.
- Reward outputs that split mixed sentences into claim and evidence components. For example, "X was associated with Y, suggesting X contributes to disease risk" should separate evidence "X was associated with Y" from claim "X contributes to disease risk."
- Penalize a claim-evidence pair when the evidence merely repeats the claim without providing an observation, measurement, statistic, result, or datum.
- Penalize claims that bundle multiple independent relations, outcomes, mechanisms, samples, thresholds, timepoints, models, or conditions into one broad claim.
- Penalize broad introductory/background claims unless the cited section presents them as this paper's own result or central conclusion with local evidence.
- Reward claims that could be internally represented as one clean subject-relation-object proposition, even though v0 does not output SPO fields.
- Treat a hypothesis as a tentative claim when the paper itself makes it.
- Treat background, prior-work context, and assumptions as non-targets unless the paper directly adopts them as part of its own contribution.
- Treat methods/results statements as evidence when they support a claim. They should only be accepted as claims when the paper is asserting that method/result as a focal contribution.
- Prefer claims that are falsifiable or testable in principle, while preserving necessary method/model context and auxiliary assumptions.

For v0, do not penalize missing or empty subject, predicate, object, ontology mappings, rich context, or details. Those fields may exist only for backward compatibility. Judge the claim-evidence pair primarily from claim_text, evidence item text, source spans/sections, and gold fields when provided.

Do not score complete coverage for an individual claim. Complete coverage is a run-level audit dimension only.
Set overall_score to the mean of accurate_extraction_score and evidence_evaluation_score.

Mode behavior:
- intrinsic_audit: evaluate the extracted claim against the source section text and extraction packet.
- gold_comparison: compare the extracted claim against the gold/reviewed source quote and gold/corrected claim text. Use SPO fields only if both sides provide meaningful values.

Do not invent missing corrections. Use suggested_corrections_json only when a correction is directly supported by the provided source or gold fields.
If the source is ambiguous, prefer "uncertain" and explain the ambiguity.
