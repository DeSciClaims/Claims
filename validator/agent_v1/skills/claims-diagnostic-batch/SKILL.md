---
name: claims_diagnostic_batch
description: Review multiple anonymized Claims miner artifacts for one paper and emit one independent rigor report per submission.
argument-hint: "<diagnostic-batch-workspace>"
allowed-tools: Read, Write, Glob, Grep
metadata:
  category: claims-validation
  version: "1.2.0"
  tags: [claims, validator, diagnostics, batch]
---

# Claims Batched Diagnostic Reviewer

Review every anonymized submission listed in `task.json`. Each submission must
be judged independently against its own artifact, linked source payload,
structural findings, and grounding findings.

Do not compare or rank submissions. Do not transfer a finding from one
submission to another. All model-facing identifiers are short opaque aliases.
Submission references, claim refs, evidence refs, and source-span refs must be
copied exactly into the output. The validator maps them back to stored artifact
identifiers.

## Required Dimensions

For each submission, review:

1. `evidence_relevance`: cited evidence substantively supports each claim.
2. `falsifiability_quality`: falsification criteria are specific and scoped.
3. `scope_calibration`: claims assert only what their evidence supports.
4. `argument_coherence`: claims, experiments, evidence, and trace align.
5. `exploration_integrity`: the trace honestly represents decisions and limits.
6. `methodological_rigor`: methods, baselines, statistics, and metrics are adequate.
7. `grounding_adjudication`: cited spans support quotes, numbers, identifiers,
   units, scope, and multi-span assertions.

Use only the linked source payload as source truth. Do not fetch external
sources. Do not repair artifacts and do not calculate final scores.

## Candidate Evidence Verdicts

For every alias listed in each submission's `required_claim_refs`, emit exactly
one `candidate_evidence` assessment. Load the model-facing artifact
programmatically and verify that the set of emitted `claim_id` values exactly
equals `required_claim_refs` before writing. Copy `claim_id` and `evidence_ids`
exactly from that claim. The validator attaches the authoritative
`evidence_ids` and `source_span_ids` from the claim after validating the output,
so these arrays may be empty in model output. Use only evidence linked by that
same claim when deciding the status; evidence belonging to another claim or
submission cannot support it.

Assign one status:

- `supported`: every material assertion in the claim is supported by its own
  linked evidence and cited source spans.
- `partially_supported`: some material assertions are supported, but the claim
  adds unsupported scope, mechanism, population, causality, quantities, or
  conclusions.
- `unsupported`: the linked evidence is irrelevant to or contradicts the claim.
- `unverifiable`: the linked evidence or source text is missing or insufficient
  to decide support.

Only `supported` claims are eligible for Silver coverage. Judge the scientific
content, not metadiscourse. Phrases such as "as reported in the cited source"
or "this claim is not generalized beyond that scope" do not establish evidence
support or scope calibration. List unsupported material assertions explicitly.

Do not write free-form evidence excerpts. Do not invent evidence IDs or span
IDs; the validator derives those fields from the stored claim links.

## Findings

- Emit only concrete issues.
- Allowed severities are `critical`, `major`, `minor`, `warning`, and `suggestion`.
- Use `critical` for unsupported or contradictory major claims.
- Include `target_type` and `target_id` whenever possible.
- Deterministic findings are context. Never suppress missing source payload,
  missing source refs, missing span IDs, or invalid source roles.
- A clearly false deterministic `quote_not_in_source` or `number_not_grounded`
  finding may be suppressed with a `suggestion` finding whose metadata contains
  `{"code":"grounding_finding_supported","suppresses_finding_id":"G001"}`.

## Output Contract

Write one strict JSON object containing exactly one report per expected
`submission_ref`:

```json
{
  "reports": [
    {
      "submission_ref": "s0",
      "candidate_evidence": [
        {
          "claim_id": "c0",
          "status": "partially_supported",
          "evidence_ids": ["e0"],
          "source_span_ids": ["p0"],
          "reason": "The evidence supports an association in the study sample but not the claim's causal conclusion.",
          "unsupported_assertions": ["The association is causal."]
        }
      ],
      "findings": [
        {
          "dimension": "scope_calibration",
          "severity": "major",
          "target_type": "claim",
          "target_id": "c0",
          "message": "The claim exceeds the population described by its evidence.",
          "evidence_span": "exact artifact excerpt",
          "suggestion": "Narrow the claim to the studied population.",
          "metadata": {}
        }
      ]
    }
  ]
}
```

Use an empty `findings` array when a submission has no concrete rigor issue,
but always return the complete `candidate_evidence` partition. The validator
will reject and repair a report that omits, duplicates, or invents claim IDs or
evidence references. When `validator_rejections` is non-empty, correct every
listed issue while still returning the complete required claim partition.
Validate the complete output against `output_schema.json` before writing it.
