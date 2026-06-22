# RFC 0003: Schema

## Core Objects

This repo uses the canonical object names from the prototype claim graph schema.

### `paper.schema.json`

`Paper` is the source document object.

### `span.schema.json`

`Span` is the provenance anchor object.

### `claim.schema.json`

`Claim` is the paper-specific assertion with a queryable subject-predicate-object core plus epistemic metadata.

The schema also supports an optional `claim_profile` field.

- `claim_kind` is the coarse semantic class
- `claim_profile` is the fine-grained extraction profile used to constrain
  expected `context` and `details`

### `evidence_item.schema.json`

`EvidenceItem` is the first-class evidence object linked to source spans.

### `claim_evidence_link.schema.json`

`ClaimEvidenceLink` is the explicit relation between a `Claim` and an `EvidenceItem`.

### `extraction.schema.json`

This is the transport bundle used in the demo repo. It groups:

- one `Paper`
- zero or more `Span` objects
- zero or more `Claim` objects
- zero or more `EvidenceItem` objects
- zero or more `ClaimEvidenceLink` objects

### `validator_score.schema.json`

A simple validator report containing:

- a paper identifier
- a total score
- score components
- an acceptance decision
- notes

## `context` vs `details`

Both `Claim` and `EvidenceItem` can carry `context` and `details`, but they are
meant to capture different things.

- `context` describes qualifiers, scope, or applicability conditions
- `details` describes the structured payload of the result or evidence itself

### Rule of thumb

Put a field in `context` if it answers:

- who does this apply to?
- where or when does this apply?
- under what condition, setting, cohort, comparator, or species does this hold?

Put a field in `details` if it answers:

- what was measured or estimated?
- what quantitative or structured result was reported?
- what model, estimator, interval, unit, or sample-size payload belongs to the result itself?

### Why the schema separates them

`context` is typed as a map of `SemanticField` objects, so it is suitable for:

- semantic qualifiers
- ontology attachment
- query filtering by applicability

`details` is intentionally more open-ended, so it is suitable for:

- numeric values
- statistical payloads
- model-specific structured data

### Canonical examples

#### Example 1: population qualifier

```json
"context": {
  "population": {
    "value": "adults with type 2 diabetes",
    "entity_type": "population",
    "ontology": null
  }
},
"details": {}
```

Interpretation:

- the claim applies to a specific population
- there is no additional structured result payload at the claim level

#### Example 2: effect estimate

```json
"context": {
  "population": {
    "value": "replication sample",
    "entity_type": "population",
    "ontology": null
  }
},
"details": {
  "effect_size": "0.022%",
  "model_type": "linear regression"
}
```

Interpretation:

- `replication sample` is where the claim applies
- `0.022%` and `linear regression` are part of the result payload

### Borderline cases

#### Population, cohort, species, setting

Usually `context`.

Examples:

- `population = older adults`
- `species = human`
- `cohort = discovery cohort`
- `setting = discovery phase GWAS`

These qualify the claim; they are not themselves the measured result.

#### Effect size, p-value, confidence interval, odds ratio

Usually `details`.

Examples:

- `effect_size = 1.8 percentage points per allele`
- `p_value = 0.01`
- `ci_low = -5.4`
- `ci_high = -0.8`
- `odds_ratio = 0.912`

These are result payload fields, not applicability qualifiers.

#### Outcome name

Usually `details` on `EvidenceItem`, sometimes omitted on `Claim` if already
captured by the SPO core.

Example:

- `details.outcome_name = HbA1c`

This is especially useful when the evidence item contains a structured
measurement summary.

#### Timepoint, comparator, dose

Usually `context`.

Examples:

- `timepoint = 12 weeks`
- `comparator = placebo`
- `dose = 10 mg daily`

These say under what study condition the evidence applies.

#### Statistical model type

Usually `details`.

Examples:

- `model_type = linear regression`
- `model_type = Cox proportional hazards model`

The model is part of how the estimate was produced, so it belongs with the
result payload.

#### Trait or phenotype phrase embedded in a qualifier

Use judgment:

- if it is the main proposition target, prefer the SPO core
- if it only narrows applicability, prefer `context`
- if it names what was measured in a result summary, prefer `details.outcome_name`

### Practical interpretation

If removing the field changes **where, when, for whom, or under what condition**
the claim holds, it belongs in `context`.

If removing the field loses **what was measured, estimated, or numerically
reported**, it belongs in `details`.

## `claim_kind` vs `claim_profile`

The current schema already distinguishes broad claim categories with
`claim_kind`, but that is not enough to determine which `context` keys and
which `details` keys are expected.

For example, all of the following may have `claim_kind = result`:

- a GWAS association result
- an intervention effect result
- a meta-analysis result
- a polygenic score result
- a descriptive cohort finding

Those are not interchangeable from a schema perspective. They expose different
expected qualifiers and different result payloads.

### Proposed interpretation

- `claim_kind` remains the broad class used for coarse miner/validator logic
- `claim_profile` becomes the more specific profile used to define:
  - allowed `context` keys
  - recommended `context` keys
  - allowed `details` keys
  - recommended `details` keys

### Why we need `claim_profile`

Without `claim_profile`:

- `context` is an open map
- `details` is an open object
- validators cannot reliably tell whether fields are missing or just absent by
  design
- ontology-routing logic has to guess which fields are expected

With `claim_profile`:

- the extractor can target a known field set
- the validator can judge omission more consistently
- review UIs can show profile-specific forms
- ontology linking can route only the relevant fields

### Proposed schema rule

`claim_profile` is an optional string in `claim.schema.json`.

It should be:

- omitted if the miner cannot determine a stable profile
- populated when the miner can assign a specific profile with reasonable
  confidence

### Example profile registry

The profile registry does not need to be hard-coded in the public JSON Schema.
It can live as a documented vocabulary used by miners and validators.

#### Profile: `intervention_effect_result`

Typical meaning:

- a treatment, exposure, or intervention changes an outcome in a defined
  population

Allowed `context` keys:

- `population`
- `species`
- `dose`
- `timepoint`
- `comparator`
- `setting`

Allowed `details` keys:

- `outcome_name`
- `effect_size`
- `unit`
- `p_value`
- `ci_low`
- `ci_high`
- `sample_size`
- `model_type`

#### Profile: `gwas_association_result`

Typical meaning:

- a variant, locus, or score is associated with a trait or outcome

Allowed `context` keys:

- `population`
- `cohort`
- `ancestry`
- `setting`
- `phenotype_context`

Allowed `details` keys:

- `variant_id`
- `effect_size`
- `p_value`
- `ci_low`
- `ci_high`
- `sample_size`
- `model_type`
- `estimator`

#### Profile: `meta_analysis_result`

Typical meaning:

- an aggregated evidence synthesis reports an effect across studies

Allowed `context` keys:

- `population`
- `setting`
- `comparator`

Allowed `details` keys:

- `outcome_name`
- `effect_size`
- `estimator`
- `p_value`
- `ci_low`
- `ci_high`
- `study_count`
- `sample_size`

#### Profile: `polygenic_score_result`

Typical meaning:

- a polygenic score or SNP-aggregate explains variance or predicts an outcome

Allowed `context` keys:

- `population`
- `cohort`
- `ancestry`
- `setting`

Allowed `details` keys:

- `score_type`
- `variance_explained`
- `effect_size`
- `sample_size`
- `model_type`

#### Profile: `descriptive_cohort_result`

Typical meaning:

- a paper reports a descriptive pattern, prevalence, or count within a cohort

Allowed `context` keys:

- `population`
- `cohort`
- `species`
- `setting`
- `timepoint`

Allowed `details` keys:

- `count`
- `proportion`
- `prevalence`
- `sample_size`

### Relationship to ontology linking

`claim_profile` is also useful for ontology routing.

Examples:

- `gwas_association_result` should strongly expect `EFO` and `STATO` usage
- `meta_analysis_result` should expect `STATO` and evidence-method mappings
- `intervention_effect_result` should expect outcome, population, and estimator
  semantics

So the ontology layer should use:

- `claim_kind` for coarse fallbacks
- `claim_profile` for profile-aware field routing

### Example

```json
{
  "claim_kind": "result",
  "claim_profile": "intervention_effect_result",
  "context": {
    "population": {
      "value": "adults with type 2 diabetes",
      "entity_type": "population",
      "ontology": null
    }
  },
  "details": {}
}
```

Interpretation:

- broad class: this is a `result`
- specific profile: it is an `intervention_effect_result`
- `population` is expected and appropriate as `context`
- empty `details` is valid here syntactically, but a validator may decide that a
  stronger payload is expected for this profile if the source span supports it

## Guiding Principle

The public schema should use the prototype claim graph vocabulary consistently and avoid parallel naming systems.
