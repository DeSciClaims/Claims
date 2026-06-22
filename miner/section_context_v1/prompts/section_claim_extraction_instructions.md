You are extracting structured scientific claims from the ORIGINAL RAW TEXT of one section of a paper.

You are also given:
- a whole-paper summary
- a section summary

These summaries are only for context.

Critical rules:
- Do NOT extract claims from the summaries.
- Do NOT treat the summaries as evidence.
- Every emitted claim, context field, details field, and evidence item must be grounded in the raw section text.
- If a scientifically interesting claim would require evidence or qualifiers from another section, skip it in this v1 pipeline.
- Prefer fewer stronger claims over many partial claims.
- Return STRICT JSON ONLY. Do not include markdown fences, explanations, or commentary.

Return STRICT JSON ONLY with keys:
- `claims`
- `evidence_items`
- `claim_evidence_links`

For each claim:
- require non-empty `claim_text`
- require non-empty `subject`, `predicate`, and `object`
- require non-empty `context`
- require non-empty `details`
- `subject`, `predicate`, and `object` must be `SemanticField` objects with:
  - `value`
  - `entity_type`
  - `ontology`
- Allowed claim `context` keys:
  - `__CLAIM_CONTEXT_KEYS__`
- Allowed claim `details` keys:
  - `__CLAIM_DETAIL_KEYS__`
- If a qualifier is really a context field, put it in `context`, not `details`.
- If a field is not supported by the raw section text, omit it rather than inventing it.

For each evidence item:
- require a concrete `summary_text`
- require concrete `details`
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
- Put conditions/applicability qualifiers in evidence `context`.
- Put measured or estimated result payload in evidence `details`.

For links:
- only link a claim to evidence that directly supports it
- use `claim_index` and `evidence_index`
- include `relation`
- include `confidence`

Important distinctions:
- `claim.context` = qualifiers for where the claim applies
- `claim.details` = structured claim-side payload such as count/effect/statistical qualifier/model qualifier
- `evidence.context` = qualifiers for the evidence conditions such as population, assay_type, timepoint, comparator, setting
- `evidence.details` = measured or estimated result payload such as outcome_name, effect_size, unit, p_value, ci_low, ci_high, sample_size

Use the summaries only to understand the paper-level context and section role.
Do not copy wording from the summaries unless the same content is explicitly grounded in the raw section text.

If no fully localizable claims exist in this section, return empty arrays.
