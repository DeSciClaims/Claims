# Ontology Context V1 Validator

## Role

Reference validator implementation for contract-level checking of ontology-aware
miner outputs.

## Input

- `ontology_context_v1_output.json`

## External References

- `contracts/claim_profiles.v1.json`
- `contracts/evidence_methods.v1.json`
- `contracts/ontology_routes.v1.json`
- `contracts/field_policies.v1.json`

## Main Checks

1. `claim_profile` exists on each claim.
2. `claim_profile` is defined in the contract file.
3. claim `context` keys are allowed for that profile.
4. claim `details` keys are allowed for that profile.
5. required claim keys are present.
6. evidence `context` keys are allowed for that `evidence_method`.
7. evidence `details` keys are allowed for that `evidence_method`.
8. required evidence keys are present.
9. ontology-target fields are not obviously raw numeric payloads.
10. ontology-target fields are not obviously long prose snippets.
11. obvious structural errors are flagged:
    - numeric object instead of payload field
    - possible subject/object inversion
12. obvious qualifier-structuring gaps are flagged:
    - modality
    - mechanism
    - temporal lag
    - scope/equilibrium/constraint qualifiers

## Output

- `ontology_context_v1_validation_report.json`
- `ontology_context_v1_validation_issues.csv`

## Why This Validator Exists

This validator is not judge v4.

It is the deterministic contract validator that sits underneath or beside a
future LLM validator.

Its purpose is to make miner outputs:

- schema-aware
- vocabulary-aware
- ontology-routing-aware
- easier to review and debug before LLM judging

## Expected Future Extension

Judge v4 can build on top of this by adding:

- stronger polarity checks against evidence links
- stronger qualifier-preservation checks
- stronger SPO role checks
- stronger ontology-target quality checks
- paper-aware semantic judging
