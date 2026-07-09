You are deciding whether a section of a paper is a good target for v0 claim-evidence extraction.

This pipeline only wants paper-owned claims that can be paired with evidence text from the same section.

A claim is the proposition to be evaluated: a checkable assertion about an
effect, relation, mechanism, comparison, tendency, hypothesis, or conclusion.
An evidence item is the information used to evaluate that proposition: an
observation, measurement, statistic, experimental result, figure/table output,
or reported datum. Background context, prior-work claims, assumptions, and
methods without result-bearing support are not extraction targets by themselves.
Use the same conservative discipline as the section-context pipeline: if a
candidate section cannot support one clean, section-local, structured
proposition with distinct supporting evidence, it should not be extracted.

Return STRICT JSON ONLY with keys:
- `should_extract`: boolean
- `reason`: short string
- `likely_claim_density`: short label such as `none`, `low`, `medium`, `high`
- `likely_evidence_density`: short label such as `none`, `low`, `medium`, `high`

Prefer `should_extract = false` when:
- the section is mostly background or boilerplate
- the section contains discussion-level interpretation without local evidence
- the section is mostly methods with no result statement
- the section contains metadata, labels, or support material that is not a claim target
- the section has assertions but no distinct evidence items to evaluate them
- the section has broad introductory claims whose evidence comes from prior work or other sections

Prefer `should_extract = true` when:
- the section contains specific results
- the section contains paper-owned claims with direct evidence text
- the section contains statistics, measurements, comparisons, thresholds, or other evidence anchors
- mixed sentences can be split into a claim proposition and a separate supporting datum
- individual findings can be separated by sample, model, threshold, timepoint, comparator, or outcome
