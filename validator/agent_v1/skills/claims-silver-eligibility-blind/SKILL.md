# Claims Silver Eligibility: Blind Tiebreak

This is a two-stage blind tiebreak for scientific claim admission.

When the task mode is `independent_discovery`, you will not receive disputed candidate text or primary judgments. Read only the supplied paper and validator-owned source spans. Reconstruct all complete, author-asserted, paper-original, sufficiently supported, faithful, author-salient findings. Candidate-produced evidence metadata is never authoritative. Each finding must cite its decisive source spans. Number real findings sequentially as `f0`, `f1`, and so on. Return an empty findings list when no finding qualifies. Never return schema examples, placeholders, or generic sample text. Do not speculate about what another agent may have proposed.

When the task mode is `candidate_resolution`, treat the locked independent findings as immutable. Review each disputed candidate against those findings, the supplied evidence, and the two primary assessments. Assess all six hard gates exactly once:

- `propositional_completeness`
- `author_assertion`
- `paper_original_support`
- `argument_sufficiency`
- `fidelity`
- `author_marked_salience`

Explain how the primary disagreement is resolved. Do not create a compromise verdict, silently rewrite a candidate, or perform candidate-relationship adjudication. A PASS requires all six gates to pass.

Return only the JSON object required by the supplied schema.
