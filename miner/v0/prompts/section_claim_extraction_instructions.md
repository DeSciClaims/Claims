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
- If a candidate would not be valid as one clean structured claim, skip it instead of emitting a broad sentence.
- Do not include markdown fences, explanations, or commentary.

Claim/evidence distinction:
- A scientific claim is a checkable proposition that asserts something about the world: an effect, relation, mechanism, comparison, tendency, hypothesis, or conclusion.
- A claim is the proposition to be evaluated.
- An evidence item is the information used to evaluate the claim: an observation, measurement, statistic, experimental result, figure/table output, or reported datum that supports, weakens, contradicts, qualifies, or fails to support the claim.
- Evidence is not the claim itself. Do not copy the same sentence into both `claim_text` and `summary_text` unless one clause is the proposition and another clause is the supporting datum.
- Prefer claims that are in principle falsifiable or testable.
- Preserve meaning-critical context, assumptions, model scope, method constraints, modality, polarity, attribution, and confidence in the claim or evidence text when they affect evaluation.
- Treat hypotheses as tentative claims when they are made by this paper.
- Treat background assumptions and prior-work context as non-targets unless this paper adopts them as part of its own contribution.
- Treat methods/results statements as evidence when they support a claim. A method/result may also be a claim only when the paper is asserting that result as a contribution.
- Split mixed sentences into separate evidence and claim records when needed.

Internal atomization discipline:
- Before emitting a claim, mentally identify a single subject, relation, and object or target, even though v0 does not output SPO fields.
- Emit one proposition per claim. A claim should not bundle multiple relations, multiple mechanisms, or multiple independent outcomes.
- If a sentence reports multiple separable findings, split them into multiple claims.
- Do not skip a multi-entity result merely because it mentions several entities. If each entity/result has its own checkable payload, split the sentence into one claim per entity/result and link each claim to the shared or entity-specific evidence.
- If a sentence reports the same relation under multiple samples, thresholds, timepoints, models, or conditions, split when the numeric payload or condition differs materially.
- If the relation depends on a mechanism or interpretation, keep the mechanism in the claim only when it is the proposition being asserted; otherwise keep the measured result as evidence.
- Do not emit broad setup claims such as "trait X is heritable" or "X is associated with social outcomes" from introductory/background material unless the local section presents them as this paper's own result or central conclusion.
- Do not emit a claim whose only evidence would be the exact same proposition restated. Either split the source into a proposition and distinct supporting datum, or skip it.

For each claim:
- Include `claim_text`.
- Make `claim_text` atomic and self-contained.
- If one sentence contains multiple separable findings, emit one claim per finding.
- Keep meaning-critical sample size, count, P value, confidence interval, effect size, odds ratio, modality, scope, comparator, and qualifier language inside `claim_text`.
- Do not make the claim more categorical than the source text.
- Keep `claim_text` to the proposition itself. Do not include the whole surrounding sentence if it contains background, evidence, method description, or explanation that belongs in evidence.
- The claim should be specific enough that a reviewer can say true/false/unsupported from the linked evidence.
- Return only the claim fields described here.

For each evidence item:
- Include `summary_text`.
- Make `summary_text` the specific evidence text that supports one or more claims.
- The evidence text should explain why the claim should be believed, rejected, or qualified.
- Evidence should be a concrete observation, measurement, statistic, result, table/figure output, model estimate, experiment, or reported datum.
- Do not use a generic interpretive sentence as evidence when it merely restates the claim.
- Preserve exact numeric/statistical payloads, sample names, model names, thresholds, timepoints, comparators, and figure/table references when they are the support for the claim.
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
- Source meaning: "Gene X increases risk of disease Y; in 10,000 participants, carriers of variant X had OR = 1.8, p < 0.01."
  - claim_text: `Gene X increases risk of disease Y.`
  - evidence summary_text: `In 10,000 participants, carriers of variant X had OR = 1.8, p < 0.01.`
- Source meaning: "X was associated with Y, suggesting X contributes to disease risk."
  - claim_text: `X contributes to disease risk.`
  - evidence summary_text: `X was associated with Y.`
- Source meaning: "We hypothesize that inflammation mediates the association."
  - claim_text: `Inflammation may mediate the association.`
- Source meaning: "Variant A and variant B are associated with trait Y, with P values p1 and p2, respectively."
  - Do not emit one bundled claim and do not skip the finding. Split it into one claim per variant.
  - claim_text: `Variant A is associated with trait Y with P value p1.`
  - evidence summary_text: `Variant A is associated with trait Y with P value p1.`
  - claim_text: `Variant B is associated with trait Y with P value p2.`
  - evidence summary_text: `Variant B is associated with trait Y with P value p2.`
- Source meaning: "Score S explains approximately 2% of variance in outcome Y in sample A and approximately 3% in sample B."
  - claim_text: `Score S explains approximately 2% of variance in outcome Y in sample A.`
  - evidence summary_text: `In sample A, Score S explains approximately 2% of variance in outcome Y.`
  - claim_text: `Score S explains approximately 3% of variance in outcome Y in sample B.`
  - evidence summary_text: `In sample B, Score S explains approximately 3% of variance in outcome Y.`
- Source meaning: "Trait A is strongly associated with social outcomes, and there is a well-documented association between trait A and health."
  - Do not emit as a v0 claim-evidence pair unless this paper reports local evidence for that association in this section.
- Source meaning: "Variant A has an odds ratio of r for outcome Y."
  - claim_text: `Variant A has an odds ratio of r for outcome Y.`
- Source meaning: "Variant A explains q% of variance in outcome Y."
  - claim_text: `Variant A explains q% of variance in outcome Y.`
- Source meaning: "Score S explains individual differences in outcome Y with R2 = r."
  - claim_text: `Score S explains individual differences in outcome Y with R2 = r.`
- Source meaning: "The upper bound for explanatory power of score S is q% (SE = s%)."
  - claim_text: `The upper bound for explanatory power of score S is q% (SE = s%).`

Use the summaries only to understand the paper-level context and section role.
Do not copy wording from the summaries unless the same content is explicitly grounded in the raw section text.

If no fully localizable claim-evidence pairs exist in this section, return empty arrays.
