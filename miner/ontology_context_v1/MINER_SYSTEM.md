# Ontology Context V1 Miner

## Role

Reference miner implementation for ontology-aware post-processing of extracted
claims.

## Input

- `section_context_v1_output.json`

## External References

- `contracts/claim_profiles.v1.json`
- `contracts/evidence_methods.v1.json`
- `contracts/ontology_routes.v1.json`
- `contracts/field_policies.v1.json`
- Supabase ontology registry

## Main Steps

1. Load the upstream extraction JSON.
2. Parse claims and evidence into shared schema objects.
3. Assign `claim_profile` if missing.
4. Classify fields by role:
   - `ontology-target`
   - `structured-payload`
   - `prose-qualifier`
5. Normalize ontology-target values into shorter concept-like lookup phrases.
6. Route eligible ontology-target fields to ontology families.
7. Resolve lexical ontology candidates from the registry.
8. Write selected or candidate ontology annotations back to the schema fields.
9. Export a mapping-review CSV with raw and normalized values.

## Main Output

- `ontology_context_v1_output.json`

This is still an extraction-style artifact, but now enriched with:

- `claim_profile`
- ontology annotations on supported semantic fields
- `ontology_mapping_summary`
- embedded `ontology_mapping_records`

## Review Output

- `ontology_mapping_records.csv`

One row per attempted mapping, including:

- `field_path`
- `field_role`
- `raw_text`
- `normalized_text`
- `normalization_status`
- `skip_reason`
- `claim_profile`
- `routed_sources`
- `mapping_status`
- selected mapping fields
- candidate mappings JSON

## Current Limitations

- `claim_profile` is inferred heuristically when upstream output lacks it
- routing is still hand-authored contract config, not learned
- lookup is lexical only
- no semantic reranking yet
