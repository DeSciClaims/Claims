# Ontology Context V1
This is a Work in Progress pipeline experimenting with ontology mapping to given ontologies. 

`ontology_context_v1` is structured explicitly as a reference
miner-plus-validator example.

The goal is to make this folder read more like the public-facing
`claims-subnet-rfc` examples:

- external contracts define what is allowed
- the miner reads those contracts and produces ontology-aware claim outputs
- the validator reads those same contracts and checks the miner output

## Folder Structure

- `contracts/claim_profiles.v1.json`
  Claim-profile vocabulary and allowed/required `context` and `details` keys.
- `contracts/evidence_methods.v1.json`
  Evidence-method vocabulary and allowed/required `context` and `details` keys.
- `contracts/ontology_routes.v1.json`
  Field-to-ontology-family routing rules.
- `contracts/field_policies.v1.json`
  Field-role classification and normalization-rule references.
- `runner.py`
  Miner implementation.
- `validator.py`
  Deterministic validator implementation.
- `config.py`
  Runtime config and contract-path resolution.
- `profile_inference.py`
  Transitional heuristics for assigning `claim_profile`.
- `registry_client.py`
  Ontology-registry client for Supabase-backed lexical lookup.
- `resolver.py`
  Mapping resolution logic.
- `export.py`
  Output writers for miner and validator artifacts.

## Architecture

## Miner

Input:

- `section_context_v1_output.json`

References:

- claim-profile contract JSON
- evidence-method contract JSON
- ontology-routing contract JSON
- external Supabase ontology registry

What it does:

1. loads claims and evidence emitted by `section_context_v1`
2. infers or preserves `claim_profile`
3. routes eligible schema fields to ontology families
4. queries the ontology registry
5. skips non-target and payload-only fields from ontology mapping
6. normalizes ontology-target values into shorter concept-like phrases
7. writes ontology annotations back to schema fields
8. emits a flat mapping-review table

Outputs:

- `ontology_context_v1_output.json`
- `ontology_mapping_records.csv`
- `manifest.json`

## Validator

Input:

- `ontology_context_v1_output.json`

References:

- the same claim-profile contract JSON
- the same evidence-method contract JSON
- the same ontology-routing contract JSON

What it does:

1. checks that `claim_profile` is present and known
2. checks allowed and required `context` keys per `claim_profile`
3. checks allowed and required `details` keys per `claim_profile`
4. checks allowed and required evidence keys per `evidence_method`
5. checks field-role consistency
6. flags ontology-target fields that still look like prose snippets or raw numeric payloads
7. flags obvious structured-semantic problems like numeric objects and possible role inversion
8. flags missing structured polarity, modality, temporal, mechanism, and scope qualifiers when claim text clearly expresses them

Outputs:

- `ontology_context_v1_validation_report.json`
- `ontology_context_v1_validation_issues.csv`
- `manifest.json`

## Why The Contracts Live Outside Prompts

This pipeline treats vocabulary and ontology policy as subnet-level references,
not prompt text.

That matters because:

- the allowed schema vocabulary should be versioned independently from prompts
- ontology families available for mapping can change over time
- field-role policy can evolve without rewriting prompts
- miners and validators should be able to point at the same contract files
- public implementers should be able to swap in their own vocabularies or
  routing rules without rewriting the pipeline logic

## Current Scope

This is still a transitional pipeline.

It currently:

- enriches `section_context_v1` outputs rather than re-extracting claims from raw
  paper inputs
- uses deterministic lexical lookup against the ontology registry
- uses heuristic `claim_profile` inference where the upstream miner does not yet
  emit `claim_profile`
- classifies fields as `ontology-target`, `structured-payload`, or
  `prose-qualifier`
- normalizes ontology-target fields before lookup
- emits explicit skip reasons for fields that should not be ontology-mapped

It does not yet:

- run a full ontology-native extraction pass from raw paper input
- run judge v4
- do embedding search or LLM reranking
- provide the review UI

## Backward Compatibility

In this repo, the package entrypoint is:

- `python -m miner.ontology_context_v1 mine`
- `python -m miner.ontology_context_v1 validate`
