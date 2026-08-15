---
name: claims-silver-comparator
description: Build a complete Bronze/reference-to-miner scientific claim comparison graph from a paper workspace.
---

# Claims Silver Comparator

Read the complete task and source-span context before producing output.

## Objective

Identify actionable scientific relationships between reference candidates and submission candidates. Review every candidate globally so split claims, refinements, and transitive restatements are considered in context.

## Rules

- Emit exactly one `candidate_reviews` row for every supplied candidate ID.
- For every emitted pair, both endpoint reviews must contain the same actionable relation to the other endpoint.
- A candidate with no actionable pair must include a specific `no_actionable_match_reason`; an empty review is not task completion.
- Build a compact index of every candidate, then compare each submission candidate with its best reference match and check each reference candidate for missed submission matches.
- Compare reference candidates only with submission candidates. Never pair two submissions or two reference candidates.
- Emit only actionable pairs: `semantic_equivalent`, `compatible_refinement`, `compatible_split_merge`, `partial_overlap`, or `contradiction`.
- Do not emit unrelated pairs merely to demonstrate they were reviewed.
- A candidate may participate in multiple pairs when the scientific relationship is genuinely many-to-many.
- Base relations on claim meaning, qualifiers, evidence, and cited source spans, not lexical similarity alone.
- Emit every pair in `mandatory_exact_restatement_pairs` as `semantic_equivalent`.
- Treat numerical or methodological differences as material when they change the scientific claim.
- Keep rationales short and specific.
- Return an empty `pairs` array only when a complete semantic review finds no actionable reference-to-submission relationship.
- Write one strict JSON object matching the supplied schema to the required output file.

Do not modify input files and do not finish before the output file validates.
