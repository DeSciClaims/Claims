---
name: claims-silver-comparator
description: Build a complete Bronze/reference-to-miner scientific claim comparison graph from a paper workspace.
---

# Claims Silver Comparator

Read the complete task and source-span context before producing output.

## Objective

Identify actionable scientific relationships between reference candidates and submission candidates. Review every candidate globally so split claims, refinements, and transitive restatements are considered in context.

## Rules

- Copy every reference candidate ID exactly once into `reviewed_reference_candidate_ids` after reviewing it.
- Emit exactly one row in `submission_reviews` for every submission candidate.
- Put each actionable reference relationship exactly once in that submission's `reference_relations`. Do not mirror or duplicate it elsewhere.
- When a submission has no actionable reference relationship, emit an empty `reference_relations` array and a specific `no_actionable_relation_reason` naming the closest reference and the material scientific difference.
- When a submission has one or more relationships, leave `no_actionable_relation_reason` empty.
- Build a compact index of every candidate, then compare each submission candidate with its best reference match and check each reference candidate for missed submission matches.
- Compare reference candidates only with submission candidates. Never pair two submissions or two reference candidates.
- Emit only actionable relations: `semantic_equivalent`, `compatible_refinement`, `compatible_split_merge`, `partial_overlap`, or `contradiction`.
- Do not emit unrelated relations merely to demonstrate candidates were reviewed.
- A shared paper, pathway, entity, disease family, or broad topic is not an actionable relation. Two claims must share a concrete scientific proposition.
- `semantic_equivalent`: each claim expresses the same scientific proposition and their material qualifiers are compatible.
- `compatible_refinement`: one claim preserves the other's concrete proposition while adding supported specificity. A specific example of a broad theme is not automatically a refinement.
- `compatible_split_merge`: one claim states substantially the same scientific content that the other side divides across multiple claims, or vice versa.
- `partial_overlap`: the claims share a material proposition but each also asserts scientifically important content not entailed by the other.
- `contradiction`: the claims make incompatible assertions about the same scientific proposition under compatible conditions.
- Different wording, detail level, or granularity does not by itself prevent a relation. If both claims assert the same mechanism or result, use equivalence, refinement, split/merge, or partial overlap as appropriate.
- Positive calibration: "ligand binding stabilizes beta-catenin and enables TCF/LEF transcription" and "receptor engagement prevents beta-catenin degradation, allowing nuclear TCF/LEF activation" share the same mechanism and should be related.
- Negative calibration: "regulated pathway activity supports lung epithelial regeneration" and "a pathway inhibitor predicted cardiovascular risk" share a pathway family but not a concrete scientific proposition and should not be related.
- A candidate with no emitted relation can still be valid and can become a miner-only or reference-only adjudication case. Omitting an unrelated pair does not mark either candidate invalid.
- Prefer omitting a speculative relation. False-positive edges distort downstream adjudication and Silver consolidation.
- A candidate may participate in multiple relations when the scientific relationship is genuinely many-to-many.
- Base relations on claim meaning, qualifiers, evidence, and cited source spans, not lexical similarity alone.
- Emit every pair in `mandatory_exact_restatement_pairs` as `semantic_equivalent`.
- Treat numerical or methodological differences as material when they change the scientific claim.
- Keep rationales short and pair-specific. Name the concrete shared proposition and the material equivalence, difference, refinement, split, or contradiction; never reuse a generic rationale across unrelated pairs.
- Before returning every submission with empty `reference_relations`, explicitly re-check the closest mechanism-level and result-level cross-side candidates. Do not apply one blanket broad-versus-specific decision to the whole candidate set.
- Before writing, verify that every reference ID appears exactly once in `reviewed_reference_candidate_ids` and every submission ID appears exactly once in `submission_reviews`.
- When `rejected_comparison_output` and `validator_rejection` are supplied without repair targets, repair every reported invariant and return the complete global output again.
- When `repair_target_candidate_ids` is non-empty, preserve the validator-owned earlier decisions. Return targeted reference IDs in `reviewed_reference_candidate_ids`, targeted submission decisions in `submission_reviews`, and only relationships involving at least one repair target.
- Write one strict JSON object matching the supplied schema to the required output file.
- Use the harness `write_file` tool directly for the final artifact. Do not use Python, `execute_code`, shell commands, or terminal tools to construct or write the JSON.

Do not modify input files and do not finish before the output file validates.
