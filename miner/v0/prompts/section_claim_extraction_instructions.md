You are extracting scientific claim-evidence pairs from the ORIGINAL RAW TEXT of one section of a paper.

You are also given:
- a whole-paper summary
- a section summary
- optional validation feedback from a previous extraction attempt

The summaries are context only. Do not extract claims from summaries, and do not treat summaries as evidence.

Return STRICT JSON ONLY with keys:
- `claims`
- `evidence_items`
- `claim_evidence_links`

Critical rules:
- Extract claims this paper is making, especially the paper's own findings, methods/results interpretations, and central conclusions.
- Do not extract claims that are only background, prior-work claims, motivation, literature review, or generic context unless the paper is directly adopting them as part of its own contribution.
- Every claim and evidence item must be grounded in the raw section text.
- If a claim needs evidence or qualifiers from another section, skip it.
- Prefer fewer stronger claim-evidence pairs over many partial pairs.
- Do not include markdown fences, explanations, or commentary.

For each claim:
- Include `claim_text`.
- Make `claim_text` atomic and self-contained.
- If one sentence contains multiple separable findings, emit one claim per finding.
- Keep meaning-critical sample size, count, P value, confidence interval, effect size, odds ratio, modality, scope, comparator, and qualifier language inside `claim_text`.
- Do not make the claim more categorical than the source text.
- Return only the claim fields described here.

For each evidence item:
- Include `summary_text`.
- Make `summary_text` the specific evidence text that supports one or more claims.
- Keep evidence local to this section.
- Include `role`, `evidence_method`, `outcome_type`, and `presentation_type` when clear.
- Prefer `evidence_method=textual_evidence` and `presentation_type=text` unless the raw section clearly indicates another presentation format.
- Return only the evidence fields described here.

For links:
- Link every claim to at least one evidence item.
- Only link a claim to evidence that directly supports it.
- Use `claim_index` and `evidence_index`.
- Include `relation`.
- Include `confidence` when possible.

Atomic claim examples:
- Source meaning: "rs11584700 has an odds ratio of 0.912 for college completion."
  - claim_text: `rs11584700 has an odds ratio of 0.912 for college completion.`
- Source meaning: "rs9320913 explains 0.022% of variance in educational years."
  - claim_text: `rs9320913 explains 0.022% of variance in educational years.`
- Source meaning: "The linear polygenic score accounts for approximately 2% of variance in educational attainment."
  - claim_text: `The linear polygenic score accounts for approximately 2% of variance in educational attainment.`
- Source meaning: "The same polygenic scores explain individual differences in cognitive function with R2 ~= 2.5%."
  - claim_text: `The same polygenic scores explain individual differences in cognitive function with R2 ~= 2.5%.`
- Source meaning: "The upper bound for explanatory power of a linear polygenic score is 22.4% (SE = 4.2%)."
  - claim_text: `The upper bound for explanatory power of a linear polygenic score is 22.4% (SE = 4.2%).`

Use the summaries only to understand the paper-level context and section role.
Do not copy wording from the summaries unless the same content is explicitly grounded in the raw section text.

If no fully localizable claim-evidence pairs exist in this section, return empty arrays.
