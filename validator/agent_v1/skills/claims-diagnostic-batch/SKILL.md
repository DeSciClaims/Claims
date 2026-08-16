---
name: claims_diagnostic_batch
description: Review multiple anonymized Claims miner artifacts for one paper and emit one independent rigor report per submission.
argument-hint: "<diagnostic-batch-workspace>"
allowed-tools: Read, Write, Glob, Grep
metadata:
  category: claims-validation
  version: "1.0.0"
  tags: [claims, validator, diagnostics, batch]
---

# Claims Batched Diagnostic Reviewer

Review every anonymized submission listed in `task.json`. Each submission must
be judged independently against its own artifact, linked source payload,
structural findings, and grounding findings.

Do not compare or rank submissions. Do not transfer a finding from one
submission to another. Submission references are opaque and must be copied
exactly into the output.

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
      "submission_ref": "S0001",
      "findings": [
        {
          "dimension": "scope_calibration",
          "severity": "major",
          "target_type": "claim",
          "target_id": "C01",
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

Use an empty `findings` array when a submission has no concrete rigor issue.
Validate the complete output against `output_schema.json` before writing it.
