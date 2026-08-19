---
name: claims_diagnostic_batch
description: Review all anonymized Claims miner artifacts for one paper and emit independent rigor findings plus compact claim assessments.
argument-hint: "<diagnostic-paper-workspace>"
allowed-tools: Read, Write, Glob, Grep
metadata:
  category: claims-validation
  version: "2.0.0"
  tags: [claims, validator, diagnostics, batch]
---

# Claims Paper Diagnostic Reviewer

Review every anonymized submission listed in `task.json`. Each submission must
be judged independently against its own artifact, linked source payload,
structural findings, and grounding findings.

Do not compare or rank submissions. Do not transfer a finding from one
submission to another. Submission references are opaque and must be copied
exactly into the output.

This is one paper-level review operation. Do not create miner shards, child
tasks, or additional agent sessions.

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

## Claim Assessments

Each submission row links to `claim_assessment_cases.json`. Read it and emit
exactly one compact assessment for every `claim_ref`. The references are local
aliases such as `c0`; copy those aliases exactly. Do not emit or reconstruct the
artifact's longer claim IDs, evidence IDs, or span IDs. The validator owns that
mapping.

For each claim assign:

- `evidence_status`: `supported`, `partially_supported`, `unsupported`, or
  `unverifiable`.
- `paper_relevance`: `central`, `supporting`, or `peripheral`.
- `priority_rank`: a positive integer ranking claims within that submission,
  where 1 is the most important.
- `reason`: one short claim-specific explanation.
- `unsupported_assertions`: only material unsupported parts of the claim.

`supported` means every material assertion is supported by evidence owned by
that claim and by the linked source spans. Evidence attached to another claim
or submission cannot repair it. Boilerplate such as "as stated in the source"
is not evidence. Use `unverifiable` when source material is absent or
insufficient to decide.

Use `central` for a paper's primary finding or contribution, `supporting` for a
material result needed to understand or support it, and `peripheral` for a
true but low-value detail. Rank claims after considering the paper's title,
abstract, claims summary, and main argument. Do not use miner-supplied
importance metadata.

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
      "claim_assessments": [
        {
          "claim_ref": "c0",
          "evidence_status": "partially_supported",
          "paper_relevance": "central",
          "priority_rank": 1,
          "reason": "The evidence supports an association but not the causal wording.",
          "unsupported_assertions": ["The association is causal."]
        }
      ],
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

Use an empty `findings` array when a submission has no concrete rigor issue,
but always return the complete `claim_assessments` partition. Validate the
complete output against `output_schema.json` before writing it.
