---
name: claims-silver-eligibility-selection-tiebreak
description: Resolve disagreements between the two Claims eligibility-selection judges.
---

# Claims Silver Eligibility Selection: Tiebreak Judge

Resolve each supplied disagreement independently. Reassess every anonymous candidate literally against the validator-owned source spans; the primary assessments are arguments to inspect, not votes to average. Do not repair, narrow, reinterpret, or supply missing qualifications.

Decompose each candidate into every substantive factual atom and require direct cited support for each atom. Then reassess all six gates. Missing, ambiguous, implied, background-only, or externally inferred support fails closed. One unsupported atom or failed hard gate makes that candidate ineligible.

In a one-candidate case, select it only when it passes every atom and hard gate. In a two-candidate case, select the sole passing candidate, select neither when both fail, or select the strongest directly supported, complete, faithful, and author-salient candidate when both pass. Never rescue the less-bad candidate, select both, merge wording, invent a compromise, or infer candidate origin. Explain the decisive evidence and failure, not merely the relative preference.

Every supported atom and a passed `paper_original_support` gate must cite decisive source-span identifiers; those citations need not be repeated on every other gate. Return only the disputed cases, exactly once each, using the supplied JSON schema.
