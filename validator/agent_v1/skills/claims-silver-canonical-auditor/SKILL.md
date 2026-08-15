---
name: claims-silver-canonical-auditor
description: Independently audit and revise a draft Silver partition before scoring.
---

# Claims Silver Canonical Auditor

Read the full paper context, accepted candidates, evidence, source spans,
adjudication consensus, canonical draft, and validator-detected issues.

## Objective

Return the corrected final Silver partition. Treat the draft as an untrusted
proposal. Do not preserve a draft unit merely because another agent emitted it.

## Required Review

- Review every draft unit ID and every accepted candidate ID.
- Inspect the full set globally for transitive duplicates, split claims,
  restatements, and refinements that do not add a separately scoreable result.
- Merge all mandatory same-unit groups. A split/restatement attack must earn one
  Silver unit and one coverage opportunity.
- Exclude every candidate listed in `mandatory_evidence_exclusions`.
- Exclude statements that are trivial, incidental, malformed, unsupported, or
  not relevant to the paper's scientific contribution.
- Check apparent contradictions against qualifiers and evidence. Do not retain
  mutually incompatible statements as independent facts without resolving the
  distinction in the canonical statements.
- Reassign `central`, `supporting`, or `minor` from the unit's role in the paper.
  Do not inherit candidate metadata or draft importance mechanically.
- Pool candidates that express one scientific unit, but keep materially distinct
  populations, interventions, mechanisms, outcomes, or qualified results apart.

## Output Contract

- Return a complete corrected partition, not comments on the draft.
- Place every accepted candidate exactly once in one final unit or one exclusion.
- Check the supplied expected counts before writing. Never omit an accepted
  candidate, even when excluding it.
- When `rejected_audit_output` and `validator_rejection` are supplied, repair
  every reported invariant and return the entire output again, not a patch.
- Set every quality-check flag to true only after performing that review.
- Record concise findings and how each was resolved.
- Preserve all anonymous IDs exactly.
- Write one strict JSON object matching the supplied schema.

Do not modify input files and do not finish before the output file validates.
