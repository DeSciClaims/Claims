You are classifying raw candidate spans and extracting scientific claim-evidence pairs from the ORIGINAL RAW TEXT of one section of a paper.

You are also given:
- a whole-paper summary
- a section summary
- candidate spans extracted from the raw section text
- optional validation feedback from a previous extraction attempt

The summaries are context only. Do not extract claims from summaries, and do not treat summaries as evidence.
Use the candidate spans as the primary units to classify, split, normalize, and link. You may consult the raw section text to resolve local context and avoid candidate-boundary mistakes.

Return STRICT JSON ONLY with keys:
- `classified_spans`
- `decomposed_units`
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

Stage 2 task:
1. Classify each candidate span as `claim`, `evidence`, `background_assumption`, `method_result`, `mixed`, or `abstain`.
2. Split `mixed` or compound candidates into smaller claim/evidence/background/method units when needed.
3. Emit every split or unsplit unit in `decomposed_units`.
4. Normalize only claim-labelled decomposed units into final `claims`.
5. Normalize only evidence-labelled decomposed units into final `evidence_items`.
6. Link each final claim to one or more direct evidence items.

For `decomposed_units`, include:
- `unit_id`: short stable ID within this section, such as `u0`, `u1`, `u2`
- `source_candidate_ids`
- `unit_text`
- `primary_label`: `claim`, `evidence`, `background_assumption`, `method_result`, or `abstain`
- `rhetorical_role`
- `claim_subtype`
- `evidence_type`
- `modality`
- `polarity`
- `attribution`
- `confidence`

Compound candidate rule:
- Splitting is required by content, not by label.
- If a candidate span contains multiple entities, variants, predictors, outcomes, samples, models, thresholds, conditions, or timepoints with distinct numeric/statistical payloads, emit separate final claims for each separable payload even if `primary_label` is `claim`.
- Do not use `primary_label=claim` as permission to keep a bundled claim.
- If a candidate span says "two loci", "three SNPs", "four outcomes", "A and B", "respectively", or gives multiple parenthetical identifiers/statistics, inspect whether each item can become its own claim.
- A count-level summary may be kept only when the exact identities or item-level payloads are not provided in the candidate or local section text.
- When both count-level and item-level information are present, prefer item-level claims.
- Final `claims` should normally be a one-to-one normalization of claim-labelled `decomposed_units`.
- Do not create a final claim that is broader or more bundled than its source decomposed unit.

For `classified_spans`, include:
- `candidate_id`
- `source_text`
- `primary_label`: `claim`, `evidence`, `background_assumption`, `method_result`, `mixed`, or `abstain`
- `rhetorical_role`: one of `background`, `hypothesis`, `method`, `experiment`, `observation`, `result`, `conclusion`, `model`, `goal`, or `unclear`
- `claim_subtype`: one of `hypothesis`, `causal`, `associational`, `mechanistic`, `comparative`, `descriptive`, `model_performance`, `none`, or `unclear`
- `evidence_type`: one of `statistic`, `figure`, `table`, `observation`, `estimate`, `dataset`, `methodological_detail`, `text`, `none`, or `unclear`
- `modality`: one of `certain`, `probable`, `possible`, `speculative`, or `unclear`
- `polarity`: one of `positive`, `negative`, `null`, `mixed`, or `unclear`
- `attribution`: one of `own_work`, `prior_literature`, `widely_accepted`, `disputed`, or `unclear`
- `confidence`

Claim/evidence distinction:
- A scientific claim is a checkable proposition that asserts something about the world: an effect, relation, mechanism, comparison, tendency, hypothesis, or conclusion.
- A claim is the proposition to be evaluated.
- An evidence item is the information used to evaluate the claim: an observation, measurement, statistic, experimental result, figure/table output, or reported datum that supports, weakens, contradicts, qualifies, or fails to support the claim.
- Evidence is not the claim itself. Do not copy the same sentence into both `claim_text` and `summary_text` unless one clause is the proposition and another clause is the supporting datum.
- Paper-owned discovery/result findings can be claims when the finding itself is the paper's contribution. For example, "we identified locus X for trait Y with P value p" should become a claim about locus X and trait Y, not only an evidence item.
- Treat item-level association, effect-size, variance-explained, odds-ratio, prediction, comparison, benchmark, replication, or robustness findings from this paper as claim units when they are asserted as results, but keep the support statistics in linked evidence unless the claim cannot be stated without the number.
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
- Split even when the candidate span was classified as `claim`; classification labels do not override atomization.
- If a sentence reports the same relation under multiple samples, thresholds, timepoints, models, or conditions, split when the numeric payload or condition differs materially.
- If the relation depends on a mechanism or interpretation, keep the mechanism in the claim only when it is the proposition being asserted; otherwise keep the measured result as evidence.
- Do not emit broad setup claims such as "trait X is heritable" or "X is associated with social outcomes" from introductory/background material unless the local section presents them as this paper's own result or central conclusion.
- Do not emit a claim whose only evidence would be the exact same proposition restated. Either split the source into a proposition and distinct supporting datum, or skip it.
- Use a two-pass reading for compound sentences:
  1. Identify every checkable proposition the paper is asserting.
  2. Identify the observation, statistic, table/figure result, or reported datum that evaluates each proposition.
- If the same sentence contains both a measured result and an interpretation, use the interpretation as the claim only when it is the proposition being asserted; use the measured result as evidence.
- If the same sentence contains only a measured result, the claim may be a concise normalized proposition, but the evidence item should preserve the source-side datum/result wording rather than merely repeat the normalized claim.

For each claim:
- Include `claim_text`.
- Include `source_candidate_ids` for the candidate spans used to make the claim.
- Include `claim_subtype`, `modality`, `polarity`, `attribution`, and `extractor_confidence` when clear.
- Make `claim_text` atomic and self-contained.
- If one sentence contains multiple separable findings, emit one claim per finding.
- Keep meaning-critical modality, scope, comparator, population, condition, and qualifier language inside `claim_text`.
- Do not put support statistics such as sample size, count, P value, confidence interval, effect size, odds ratio, R2, standard error, or variance-explained percentage in `claim_text` unless the proposition being asserted is specifically about that numeric quantity.
- For association, discovery, prediction, replication, robustness, and result claims, put the proposition in `claim_text` and put supporting statistics in linked evidence.
- Do not make the claim more categorical than the source text.
- Keep `claim_text` to the proposition itself. Do not include the whole surrounding sentence if it contains background, evidence, method description, or explanation that belongs in evidence.
- The claim should be specific enough that a reviewer can say true/false/unsupported from the linked evidence.
- Return only the claim fields described here.

For each evidence item:
- Include `summary_text`.
- Include `source_candidate_ids` for the candidate spans used to make the evidence item.
- Include `evidence_type`, `rhetorical_role`, and `extractor_confidence` when clear.
- Make `summary_text` the specific evidence text that supports one or more claims.
- The evidence text should explain why the claim should be believed, rejected, or qualified.
- Evidence should be a concrete observation, measurement, statistic, result, table/figure output, model estimate, experiment, or reported datum.
- Do not use a generic interpretive sentence as evidence when it merely restates the claim.
- Do not make `summary_text` a polished duplicate of `claim_text`. Prefer source-like evidence wording that includes the datum, measurement, statistic, table/figure result, sample, model, or experimental observation.
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
  - decomposed unit: `Variant A is associated with trait Y.`
  - decomposed unit: `Variant B is associated with trait Y.`
  - claim_text: `Variant A is associated with trait Y.`
  - evidence summary_text: `The section reports Variant A among the associations for trait Y, with P value p1.`
  - claim_text: `Variant B is associated with trait Y.`
  - evidence summary_text: `The section reports Variant B among the associations for trait Y, with P value p2.`
- Source meaning: "Score S explains approximately 2% of variance in outcome Y in sample A and approximately 3% in sample B."
  - decomposed unit: `Score S explains variance in outcome Y in sample A.`
  - decomposed unit: `Score S explains variance in outcome Y in sample B.`
  - claim_text: `Score S explains variance in outcome Y in sample A.`
  - evidence summary_text: `The reported variance explained for Score S in sample A is approximately 2% for outcome Y.`
  - claim_text: `Score S explains variance in outcome Y in sample B.`
  - evidence summary_text: `The reported variance explained for Score S in sample B is approximately 3% for outcome Y.`
- Source meaning: "Trait A is strongly associated with social outcomes, and there is a well-documented association between trait A and health."
  - Do not emit as a v0 claim-evidence pair unless this paper reports local evidence for that association in this section.
- Source meaning: "Variant A has an odds ratio of r for outcome Y."
  - claim_text: `Variant A is associated with outcome Y.`
  - evidence summary_text: `The section reports an odds ratio of r for Variant A and outcome Y.`
- Source meaning: "Variant A corresponds to a d percentage-point difference in outcome Y."
  - claim_text: `Variant A is associated with outcome Y.`
  - evidence summary_text: `The section reports Variant A corresponds to a d percentage-point difference in outcome Y.`
- Source meaning: "Variant A explains q% of variance in outcome Y."
  - claim_text: `Variant A explains variance in outcome Y.`
  - evidence summary_text: `The section reports q% variance explained by Variant A for outcome Y.`
- Source meaning: "Score S explains individual differences in outcome Y with R2 = r."
  - claim_text: `Score S explains individual differences in outcome Y.`
  - evidence summary_text: `The section reports R2 = r for Score S predicting individual differences in outcome Y.`
- Source meaning: "The upper bound for explanatory power of score S is q% (SE = s%)."
  - claim_text: `Score S has an upper bound for explanatory power.`
  - evidence summary_text: `The section reports an upper bound of q% with SE = s% for the explanatory power of Score S.`

Use the summaries only to understand the paper-level context and section role.
Do not copy wording from the summaries unless the same content is explicitly grounded in the raw section text.

If no fully localizable claim-evidence pairs exist in this section, return empty arrays.
