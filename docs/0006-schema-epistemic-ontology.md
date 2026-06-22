# RFC 0006: Schema Epistemic Ontology

This document defines the current controlled-vocabulary layer for the general
`claims_subnet` schema.

It is meant to answer a simple question:

- which fields in the schema are supposed to draw from a controlled vocabulary

This is the schema-level reference, separate from prompts.

## Scope

This document covers the shared schema objects used across the public Claims
schema.

The main schema objects are:

- `Claim`
- `EvidenceItem`
- `ClaimEvidenceLink`
- `SemanticField`
- `OntologyAnnotation`

## Vocabulary Types

There are three kinds of vocabulary in the schema.

### 1. Closed Vocabulary

Values should come from a fixed documented list.

Examples:

- `evidence_method`
- `outcome_type`
- `presentation_type`

### 2. Semi-Controlled Vocabulary

Values come from a documented set of allowed keys, but the values inside those
keys are still open text or semantic fields.

Examples:

- `claim.context` keys
- `claim.details` keys
- `evidence.context` keys
- `evidence.details` keys

### 3. Open Text With Intended Semantics

The field is not a closed enum, but we still expect stable patterns.

Examples:

- `claim_profile`
- `claim_kind`
- `epistemic_status`
- `support_origin`
- `relation`

## Controlled Vocabulary By Schema Object

## `SemanticField`

`SemanticField` has:

- `value`
- `entity_type`
- `ontology`

### `entity_type`

This is semi-controlled.

Observed and intended values include:

- `population`
- `species`
- `setting`
- `timepoint`
- `comparator`
- `cohort`
- `condition`
- `modality`
- `threshold`
- `subset`
- `analysis_context`
- `phenotype_context`
- `intervention`
- `citation_context`
- `evidence_method`
- `outcome_type`
- `presentation_type`
- `trait`
- `phenotype`
- `measurement`
- `outcome`
- `assay`
- `statistic`
- `statistical_model`
- `effect_size`

This is not yet a strict closed enum in the schema, but it should behave like a
controlled semantic label.

## `Claim`

Fields:

- `claim_text`
- `subject`
- `predicate`
- `object`
- `claim_kind`
- `claim_profile`
- `epistemic_status`
- `support_origin`
- `context`
- `details`

### `claim_kind`

Open text today, but intended to be coarse and stable.

Current common value:

- `result`

Future examples:

- `result`
- `mechanistic_claim`
- `theoretical_proposition`
- `descriptive_statement`
- `causal_claim`

### `claim_profile`

Open text in the schema, but intended to come from a documented controlled set.

Current working examples from the project include:

- `generic_result`
- `gwas_association_result`
- `polygenic_score_result`
- `intervention_effect_result`
- `meta_analysis_result`
- `theoretical_proposition`

### `epistemic_status`

Open text today, but should remain a small stable set.

Recommended values:

- `asserted`
- `hedged`
- `hypothesized`
- `derived`
- `interpreted`

### `support_origin`

Open text today, but should remain a small stable set.

Recommended values:

- `text`
- `table`
- `figure`
- `caption`
- `supplement`
- `mixed`

### `claim.context` Keys

These keys are semi-controlled.

Base allowed keys in the current schema vocabulary:

- `population`
- `species`
- `setting`
- `timepoint`
- `comparator`
- `cohort`
- `condition`
- `modality`
- `threshold`
- `subset`
- `analysis_context`
- `phenotype_context`
- `intervention`
- `citation_context`

Project extensions already used in profile work:

- `ancestry`
- `replication_stage`
- `subgroup`
- `mechanism`

### `claim.details` Keys

These keys are semi-controlled.

Base allowed keys in the current schema vocabulary:

- `effect_size`
- `statistical_significance`
- `count`
- `sample_size`
- `study_count`
- `model_type`
- `confidence_qualifier`
- `directionality_note`

Project extensions already used in profile work:

- `estimator`
- `p_value`
- `ci_low`
- `ci_high`
- `variance_explained`
- `lag`
- `constraint_note`
- `outcome_name`
- `unit`
- `score_type`
- `variant_id`
- `locus`
- `gene_symbol`

## `EvidenceItem`

Fields:

- `role`
- `summary_text`
- `evidence_method`
- `outcome_type`
- `presentation_type`
- `context`
- `details`

### `role`

Open text today, but should remain a small stable set.

Recommended values:

- `support`
- `measurement`
- `background`
- `method`
- `result`

### `evidence_method`

This is the main closed vocabulary controller for evidence items.

It is a **two-level hierarchy**: each evidence method belongs to exactly one
top-level category that groups methods by epistemic mode. The method `id` is the
canonical machine value stored in the schema; `label` is the human-readable form.

```yaml
evidence_method:
  formal:
    label: Formal
    methods:
      - { id: theorem,    label: "Theorem" }
      - { id: proof,      label: "Proof" }
      - { id: derivation, label: "Derivation" }
  theoretical:
    label: Theoretical
    methods:
      - { id: verbal_argument,   label: "Verbal argument" }
      - { id: mechanistic_model, label: "Mechanistic model" }
      - { id: analytical_model,  label: "Analytical model" }
  empirical_observational:
    label: Empirical Observational
    methods:
      - { id: case_study,            label: "Case study" }
      - { id: cross_sectional,       label: "Cross-sectional" }
      - { id: case_control,          label: "Case-control" }
      - { id: longitudinal,          label: "Longitudinal" }
      - { id: panel,                 label: "Panel" }
      - { id: descriptive_statistic, label: "Descriptive statistic" }
  empirical_correlational:
    label: Empirical Correlational
    methods:
      - { id: comparison,       label: "Comparison" }
      - { id: correlation,      label: "Correlation" }
      - { id: regression,       label: "Regression" }
      - { id: data_aggregation, label: "Data aggregation" }
  empirical_quasi_experimental:
    label: Empirical Quasi-Experimental
    methods:
      - { id: instrumental_variable,    label: "Instrumental variable" }
      - { id: regression_discontinuity, label: "Regression discontinuity" }
      - { id: difference_in_difference, label: "Difference-in-difference" }
      - { id: natural_experiment,       label: "Natural experiment" }
  empirical_experimental:
    label: Empirical Experimental
    methods:
      - { id: laboratory_experiment,       label: "Laboratory experiment" }
      - { id: field_experiment,            label: "Field experiment" }
      - { id: randomized_controlled_trial, label: "Randomized controlled trial" }
  computational:
    label: Computational
    methods:
      - { id: simulation,             label: "Simulation" }
      - { id: agent_based_model,      label: "Agent-based model" }
      - { id: machine_learning_model, label: "Machine-learning model" }
  synthetic:
    label: Synthetic
    methods:
      - { id: literature_review,    label: "Literature review" }
      - { id: meta_analysis,        label: "Meta-analysis" }
      - { id: replication_analysis, label: "Replication analysis" }
  citation_based:
    label: Citation-Based
    methods:
      - { id: cites_supporting_work,     label: "Cites supporting work" }
      - { id: cites_conflicting_work,    label: "Cites conflicting work" }
      - { id: cites_methodological_work, label: "Cites methodological work" }
```

### `outcome_type`

Closed vocabulary in the schema:

- `clinical_outcome`
- `behavioral_outcome`
- `molecular_binding`
- `gene_expression`
- `physiological_measure`
- `phenotype`
- `adverse_event`
- `mechanistic_pathway`
- `structural_measure`
- `quantitative_measure`

### `presentation_type`

Closed vocabulary in the schema:

- `text`
- `table`
- `figure`
- `caption`
- `supplement`

### `evidence.context` Keys

These keys are semi-controlled and are governed primarily by `evidence_method`.

Union of keys currently used across evidence-method profiles:

- `population`
- `species`
- `comparator`
- `dose`
- `timepoint`
- `setting`
- `assay_type`
- `temperature`
- `ph`
- `citation_context`
- `section_type`
- `model_system`

### `evidence.details` Keys

These keys are semi-controlled and are governed primarily by `evidence_method`.

Union of keys currently used across evidence-method profiles:

- `outcome_name`
- `effect_size`
- `unit`
- `ci_low`
- `ci_high`
- `p_value`
- `sample_size`
- `measurement_type`
- `value`
- `target`
- `estimator`
- `correlation_value`
- `observation_type`
- `model_name`
- `assumptions`
- `replication_target`
- `study_count`
- `review_scope`
- `argument_type`
- `derivation_kind`
- `equation_refs`

## `ClaimEvidenceLink`

### `relation`

Open text today, but it should remain a small stable set.

Recommended values:

- `supports`
- `measures`
- `qualifies`
- `contextualizes`
- `contradicts`

## Field Role Guidance

Across the general schema, semantic content should be thought of in three roles.

### Ontology-Target

Short concept-like values suitable for ontology mapping.

Typical places:

- `claim.subject`
- `claim.object`
- `claim.context.phenotype_context`
- `claim.details.model_type`
- `claim.details.estimator`
- `evidence.evidence_method`
- `evidence.outcome_type`
- `evidence.presentation_type`
- `evidence.details.measurement_type`

### Structured Payload

Quantitative or structured result content that should usually not be ontology
mapped directly.

Typical places:

- `claim.details.effect_size`
- `claim.details.sample_size`
- `claim.details.count`
- `claim.details.study_count`
- `claim.details.p_value`
- `claim.details.ci_low`
- `claim.details.ci_high`
- `evidence.details.value`
- `evidence.details.effect_size`
- `evidence.details.sample_size`

### Prose Qualifier

Natural-language qualifiers that preserve nuance but are often not clean
ontology targets in their raw form.

Typical places:

- `claim.context.cohort`
- `claim.context.condition`
- `claim.context.modality`
- `claim.context.subset`
- `claim.details.directionality_note`
- `claim.details.confidence_qualifier`
- `evidence.context.citation_context`
- `evidence.details.assumptions`

## Practical Rule Of Thumb

If you are filling the schema:

- use closed vocab where the schema already expects a named category
- use only documented keys in `context` and `details`
- put numeric/statistical payload in `details`
- keep raw nuanced qualifiers out of ontology-target fields
- use `evidence_method` to decide which evidence `context` and `details` keys
  are appropriate
- use `claim_profile` to decide which claim `context` and `details` keys are
  appropriate
