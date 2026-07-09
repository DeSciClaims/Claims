You are deciding whether a section of a paper is a good target for v0 claim-evidence extraction.

This pipeline only wants paper-owned claims that can be paired with evidence text from the same section.

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

Prefer `should_extract = true` when:
- the section contains specific results
- the section contains paper-owned claims with direct evidence text
- the section contains statistics, measurements, comparisons, thresholds, or other evidence anchors
