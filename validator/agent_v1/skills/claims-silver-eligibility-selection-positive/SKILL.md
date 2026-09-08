---
name: claims-silver-eligibility-selection-positive
description: Confirm eligibility and select the best supported representative in each claim case.
---

# Claims Silver Eligibility Selection: Positive Judge

Review each one- or two-candidate case independently. Evaluate each proposition literally and exactly as submitted. Do not search for a charitable reading, repair wording, narrow scope, add a missing qualifier, or replace an unsupported assertion with a nearby supported one.

First decompose each candidate into every substantive factual atom. An atom is supported only when the supplied paper spans directly establish its complete wording and scientific force. Cite that evidence for each supported atom. Mark an atom unsupported when evidence is missing, ambiguous, merely suggestive, inherited from background literature, or requires outside knowledge. One unsupported atom makes the candidate ineligible.

Assess all six supplied eligibility gates independently for every candidate. Missing qualifiers, inflated causality or certainty, overgeneralization, incidental observations, method parameters, and claims not asserted by the authors must fail the relevant gate. Uncertainty must fail closed.

For a one-candidate case, select it only when it passes every atom and gate. For a two-candidate case, choose the sole passing candidate, select neither when both fail, or choose the candidate that most completely and faithfully represents the author-supported finding when both pass. A longer or more specific claim is better only when every added atom is directly supported. Never rescue a candidate because it is comparatively less weak.

Never select both, combine candidates, repair omissions, import outside knowledge, or prefer a candidate based on its hidden origin. Every supported atom and a passed `paper_original_support` gate must cite decisive source-span identifiers; those citations need not be repeated on every other gate.

Return every requested case exactly once and follow the supplied JSON schema.
