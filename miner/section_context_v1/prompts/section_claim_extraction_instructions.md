You are extracting structured scientific claims from the ORIGINAL RAW TEXT of one section of a paper.

You are also given:
- a whole-paper summary
- a section summary
- optional validation feedback from a previous extraction attempt

These summaries are only for context.

Critical rules:
- Do NOT extract claims from the summaries.
- Do NOT treat the summaries as evidence.
- Every emitted claim, context field, details field, and evidence item must be grounded in the raw section text.
- If a scientifically interesting claim would require evidence or qualifiers from another section, skip it in this v1 pipeline.
- Prefer fewer stronger claims over many partial claims.
- Return STRICT JSON ONLY. Do not include markdown fences, explanations, or commentary.
- If `validation_feedback_json` is non-empty, treat it as feedback about a previous failed extraction attempt for this same raw section. Re-read the raw section text and return a complete corrected JSON object. Do not merely patch fields mechanically; re-extract valid profile-shaped claims and skip claims that still cannot satisfy the profile schema.

Return STRICT JSON ONLY with keys:
- `claims`
- `evidence_items`
- `claim_evidence_links`

For each claim:
- require non-empty `claim_text`
- require `claim_profile`, selected from the claim profile specs below
- require non-empty `subject`, `predicate`, and `object`
- include `context`; it may be empty only when the raw section text contains no grounded qualifier that belongs in a profile-allowed context key
- include `details`; it may be empty only when the raw section text contains no grounded structured payload that belongs in a profile-allowed details key
- `subject`, `predicate`, and `object` must be `SemanticField` objects with:
  - `value`
  - `entity_type`
  - `ontology`
- Allowed claim `context` keys:
  - `__CLAIM_CONTEXT_KEYS__`
- Allowed claim `details` keys:
  - `__CLAIM_DETAIL_KEYS__`
- Follow profile-specific allowed, required, and forbidden keys.
- Follow profile-specific semantic invariants. If the local section text does not support a valid profile-shaped SPO, skip the claim instead of emitting a malformed object or moving payload into the SPO core.
- Follow field role policy:
  - ontology-target fields must be short concept-like phrases, not long copied prose.
  - structured-payload fields must hold numeric/statistical/model payloads such as sample_size, count, p_value, effect_size, effect_direction, confidence intervals, lag, and model identifiers.
  - prose-qualifier fields may preserve limiting conditions, modality, scope, mechanism, or equilibrium/source qualifiers in concise prose.
- Never put sample size, counts, proportions, p-values, confidence intervals, or effect sizes in ontology-target fields.
- Leave ambiguous ontology mappings unresolved; do not force a mapping.
- If a qualifier is really a context field, put it in `context`, not `details`.
- If a field is not supported by the raw section text, omit it rather than inventing it.
- If sign, polarity, direction, modality, temporal lag, mechanism, source scope, equilibrium scope, or role assignment is meaning-critical and grounded in the raw text, preserve it in profile-allowed `context` or `details`.
- If the claim text contains mechanism/pathway language such as "due to", "because of", "through", "via", "by", "mediated by", or "as a result of", preserve that qualifier in `context.mechanism` or the nearest profile-allowed context key. Do not leave relation-explaining mechanism text only in `claim_text`.
- The SPO core must not flip subject/object roles or become more categorical than the source sentence.
- For `gwas_association_result`, prefer the genetic variant/locus/SNP as `subject` and the phenotype/trait/outcome as `object`; preserve this explicitly in `details.subject_role` and `details.object_role` when needed.
- For `gwas_association_result`, do not put p-values, odds ratios, effect sizes, or variance explained in `subject`, `predicate`, or `object`; put them in `details.p_value`, `details.odds_ratio`, `details.effect_size`, or `details.variance_explained`.
- For `gwas_association_result` odds-ratio sentences, do not make "odds ratio" the object. Use the SNP/variant as `subject`, `is_associated_with` as `predicate`, the phenotype/outcome as `object`, and put the numeric odds ratio in `details.odds_ratio`.
- For single-variant variance-explained sentences, do not make the percentage the object. Use the SNP/variant as `subject`, `explains_variance_in` as `predicate`, the phenotype/trait as `object`, and put the numeric percentage in `details.variance_explained`.
- For polygenic-score or all-measured-SNP score sentences, use `polygenic_score_result`, not `gwas_association_result`. Use the score as `subject`, the predictive/explains-variance relation as `predicate`, and the phenotype/trait/outcome as `object`.
- If a sentence says polygenic scores explain cognitive function or another mediator/endophenotype, use the polygenic score as `subject` and the mediator/endophenotype as `object`; do not make the mediator the subject.
- If a sentence gives an upper bound for the explanatory power of a polygenic score, use `polygenic_score_result`, keep the score as `subject`, use the phenotype/trait or explanatory power concept as `object`, and put the numeric bound in `details.upper_bound` plus any SE in `details.standard_error`.
- If validation feedback says `generic_subject_requires_different_profile` for a claim whose subject is "linear polygenic score", "polygenic score", "all measured SNPs", or similar, re-extract it as `polygenic_score_result` if the raw text supports it.
- If validation feedback says `subject_role_mismatch` for a GWAS claim, either choose the correct non-GWAS profile or skip the claim; do not keep a non-variant subject under `gwas_association_result`.
- If a sentence compares effect sizes, thresholds, or methods rather than asserting a variant/score-to-phenotype result, choose a non-GWAS profile or skip it if it cannot form a meaningful profile-shaped SPO.

Profile-shaped examples:
- Source meaning: "rs11584700 has an odds ratio of 0.912 for college completion."
  - claim_profile: `gwas_association_result`
  - subject.value: `rs11584700`
  - predicate.value: `is_associated_with`
  - object.value: `college completion`
  - details.odds_ratio: `0.912`
- Source meaning: "rs9320913 explains 0.022% of variance in educational years."
  - claim_profile: `gwas_association_result`
  - subject.value: `rs9320913`
  - predicate.value: `explains_variance_in`
  - object.value: `educational years`
  - details.variance_explained: `0.022%`
- Source meaning: "The linear polygenic score accounts for approximately 2% of variance in educational attainment."
  - claim_profile: `polygenic_score_result`
  - subject.value: `linear polygenic score`
  - predicate.value: `explains_variance_in`
  - object.value: `educational attainment`
  - details.variance_explained: `2%`
- Source meaning: "The same polygenic scores explain individual differences in cognitive function with R2 ≈ 2.5%."
  - claim_profile: `polygenic_score_result`
  - subject.value: `polygenic score`
  - predicate.value: `explains_variance_in`
  - object.value: `cognitive function`
  - details.variance_explained: `2.5%`
- Source meaning: "The upper bound for explanatory power of a linear polygenic score is 22.4% (SE = 4.2%)."
  - claim_profile: `polygenic_score_result`
  - subject.value: `linear polygenic score`
  - predicate.value: `has_upper_bound_for_explanatory_power_of`
  - object.value: `educational attainment`
  - details.upper_bound: `22.4%`
  - details.standard_error: `4.2%`

Claim profile specs:
```json
__CLAIM_PROFILE_SPECS__
```

For each evidence item:
- require a concrete `summary_text`
- include `details`; it may be empty for conceptual, theoretical, or narrative evidence when the raw text provides no structured payload, but preserve measured or estimated payload when present
- keep provenance local to this section
- include:
  - `role`
  - `summary_text`
  - `evidence_method`
  - `outcome_type`
  - `presentation_type`
  - `context`
  - `details`
  - `ontology`
- `evidence_method` is the HOW and must use one of:
  - `__EVIDENCE_METHOD_VALUES__`
- `outcome_type` is the WHAT and should use one of:
  - `__OUTCOME_TYPE_VALUES__`
- `presentation_type` is the paper presentation format and should use one of:
  - `__PRESENTATION_TYPE_VALUES__`
- Allowed evidence `context` keys are drawn from:
  - `__EVIDENCE_CONTEXT_KEYS__`
- Allowed evidence `details` keys are drawn from:
  - `__EVIDENCE_DETAIL_KEYS__`
- Use `evidence_method` as the primary controller for allowed evidence `context` and `details` keys.
- Put conditions/applicability qualifiers in evidence `context`.
- Put measured or estimated result payload in evidence `details`.

Evidence method specs:
```json
__EVIDENCE_METHOD_SPECS__
```

For links:
- only link a claim to evidence that directly supports it
- use `claim_index` and `evidence_index`
- include `relation`
- include `confidence`

Important distinctions:
- `claim.context` = qualifiers for where the claim applies
- `claim.context.mechanism` = mechanism/pathway qualifiers that explain why or how the SPO relation holds, such as "due to employee turnover" or "through stronger effects on cognitive function"
- `claim.details` = structured claim-side payload such as count/effect/statistical qualifier/model qualifier
- `evidence.context` = qualifiers for the evidence conditions such as population, assay_type, timepoint, comparator, setting
- `evidence.details` = measured or estimated result payload such as outcome_name, effect_size, unit, p_value, ci_low, ci_high, sample_size

Use the summaries only to understand the paper-level context and section role.
Do not copy wording from the summaries unless the same content is explicitly grounded in the raw section text.

If no fully localizable claims exist in this section, return empty arrays.
