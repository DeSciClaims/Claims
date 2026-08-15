---
name: claims-silver-comparator
description: Build a complete Bronze/reference-to-miner scientific claim comparison graph from a paper workspace.
---

# Claims Silver Comparator

Read the complete task and source-span context before producing output.

## Objective

Identify actionable scientific relationships between reference candidates and submission candidates. Review every candidate globally so split claims, refinements, and transitive restatements are considered in context.

## Rules

- Preserve every supplied candidate ID exactly in `reviewed_candidate_ids`.
- Compare reference candidates only with submission candidates. Never pair two submissions or two reference candidates.
- Emit only actionable pairs: `semantic_equivalent`, `compatible_refinement`, `compatible_split_merge`, `partial_overlap`, or `contradiction`.
- Do not emit unrelated pairs merely to demonstrate they were reviewed.
- A candidate may participate in multiple pairs when the scientific relationship is genuinely many-to-many.
- Base relations on claim meaning, qualifiers, evidence, and cited source spans, not lexical similarity alone.
- Treat numerical or methodological differences as material when they change the scientific claim.
- Keep rationales short and specific.
- Write one strict JSON object matching the supplied schema to the required output file.

Do not modify input files and do not finish before the output file validates.
