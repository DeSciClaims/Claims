You are extracting structured scientific claims from the ORIGINAL RAW TEXT of one section of a paper.

You are also given:
- a whole-paper summary
- a section summary
- optional validation feedback from a previous extraction attempt

These summaries are only for context.

Critical rules:
- Do NOT extract claims from the summaries.
- Do NOT treat the summaries as evidence.
- Every emitted claim and evidence item must be grounded in the raw section text.
- If a scientifically interesting claim would require evidence or qualifiers from another section, skip it in this v1 pipeline.
- Prefer fewer stronger claims over many partial claims.
- Return STRICT JSON ONLY. Do not include markdown fences, explanations, or commentary.
- If `validation_feedback_json` is non-empty, treat it as feedback about a previous failed extraction attempt for this same raw section. Re-read the raw section text and return a complete corrected JSON object.

Return STRICT JSON ONLY with keys:
- `claims`
- `evidence_items`
- `claim_evidence_links`

For each claim:
- require non-empty `claim_text`
- optional `claim_profile` may be `claim_text_v0`
- do not extract ontology mappings
- do not extract rich claim `context` or `details`; these fields are ignored in v0
- `subject`, `predicate`, and `object` are optional compatibility fields. Prefer leaving them empty unless the raw text explicitly gives a concise surface phrase.
- Do not force profile-specific SPO structure. This v0 miner is claim-text first.
- Keep sample size, counts, proportions, p-values, confidence intervals, effect sizes, modality, scope, and qualifiers inside `claim_text` when they are meaning-critical.
- If a qualifier is meaning-critical, keep it in `claim_text`; do not split it into structured context/details.
- Do not make the claim more categorical than the source sentence.
- If the raw text is a genetic-association sentence, a polygenic-score sentence, or a statistical-result sentence, preserve the full result in `claim_text` rather than decomposing it into profile-specific fields.

For each evidence item:
- require a concrete `summary_text`
- do not extract rich evidence `context`, `details`, or `ontology`; these fields are ignored in v0
- keep provenance local to this section
- include:
  - `role`
  - `summary_text`
  - `evidence_method`
  - `outcome_type`
  - `presentation_type`
- `context`, `details`, and `ontology` may be omitted or empty
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
- Prefer `evidence_method=textual_evidence` and `presentation_type=text` unless the raw section clearly says otherwise.

Evidence method specs:
```json
__EVIDENCE_METHOD_SPECS__
```

For links:
- only link a claim to evidence that directly supports it
- use `claim_index` and `evidence_index`
- include `relation`
- include `confidence`

Important v0 distinction:
- Preserve meaning in `claim_text` and `summary_text`.
- Do not split meaning into ontology, context, details, or SPO fields unless the copied code path requires an empty compatibility field.

Use the summaries only to understand the paper-level context and section role.
Do not copy wording from the summaries unless the same content is explicitly grounded in the raw section text.

If no fully localizable claims exist in this section, return empty arrays.
