---
name: claims-silver-eligibility-selection-negative
description: Decide eligibility and select the strongest representative in each scientific claim case.
---

# Claims Silver Eligibility Selection: Negative Judge

Review each one- or two-candidate case independently. Evaluate each candidate exactly as submitted. Do not repair, narrow, reinterpret, or supply a missing qualifier, premise, comparison, or causal link.

First decompose each candidate into every substantive factual atom. For each atom, cite source spans that directly support the complete atom. Mark it unsupported when support is absent, ambiguous, merely implied, drawn from cited background literature, or dependent on outside knowledge. One unsupported atom makes the whole candidate ineligible.

Then assess all six supplied eligibility gates for every candidate using only the validator-owned source spans. Fail the relevant gate for missing scope or qualifiers, inflated certainty or causality, unsupported generalization or mechanism, incidental setup details, background claims, weak original support, or a conclusion the authors do not make. Uncertainty is a failure, not permission to infer support.

For a one-candidate case, select it only when it passes every atom and hard gate; otherwise select neither. For a two-candidate case, select the sole passing candidate, select neither when both fail, or select the stronger representative when both pass. Never pass a weak candidate merely because it is better than its partner. Never select both, merge wording, rewrite a candidate, or create a compromise claim. Do not prefer a candidate because it is reference or submission material; those identities are hidden.

Every supported atom and a passed `paper_original_support` gate must cite decisive source-span identifiers. Those citations need not be repeated on every other gate. Return every requested case exactly once and follow the supplied JSON schema.
