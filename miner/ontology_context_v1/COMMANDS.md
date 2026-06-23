# Ontology Context V1 Commands

All commands below assume:

```bash
cd /Users/ogbanugot/Workspace/claims/Claims
```

## 1. Run The Miner

Preferred command:

```bash
python -m miner.ontology_context_v1 mine \
  --extraction-output-json miner/section_context_v1/outputs/section_context_v1/<paper>/section_context_v1_output.json \
  --output-dir miner/ontology_context_v1/outputs/ontology_context_v1/<paper>
```

## 2. Run The Validator

```bash
python -m miner.ontology_context_v1 validate \
  --ontology-output-json miner/ontology_context_v1/outputs/ontology_context_v1/<paper>/ontology_context_v1_output.json \
  --output-dir miner/ontology_context_v1/outputs/ontology_context_v1/<paper>/validator_contract_report
```

## 3. Override Contract Files

The miner and validator read external contract files through env vars.

```bash
export SUBNET_CLAIMS_ONTOLOGY_CLAIM_PROFILES_PATH=/abs/path/to/claim_profiles.v1.json
export SUBNET_CLAIMS_ONTOLOGY_EVIDENCE_METHODS_PATH=/abs/path/to/evidence_methods.v1.json
export SUBNET_CLAIMS_ONTOLOGY_ROUTES_PATH=/abs/path/to/ontology_routes.v1.json
export SUBNET_CLAIMS_ONTOLOGY_FIELD_POLICIES_PATH=/abs/path/to/field_policies.v1.json
```

Then rerun the miner or validator normally.

## 4. Required Runtime Configuration

For the miner:

```bash
export SUPABASE_URL=...
export SUPABASE_SERVICE_ROLE_KEY=...
```

The validator does not query Supabase directly, but it still uses the same
config loader and contract paths.

## 5. Miner Outputs

- `ontology_context_v1_output.json`
- `ontology_mapping_records.csv`
- `manifest.json`

## 6. Validator Outputs

- `ontology_context_v1_validation_report.json`
- `ontology_context_v1_validation_issues.csv`
- `manifest.json`

## 7. Mapping Review Columns

The miner CSV now includes:

- `field_role`
- `raw_text`
- `normalized_text`
- `normalization_status`
- `skip_reason`
- `mapping_status`

This is the main review surface for ontology-target quality and mapping policy.

## 8. Example Full Flow

```bash
python -m miner.ontology_context_v1 mine \
  --extraction-output-json miner/section_context_v1/outputs/section_context_v1/Rietveld_et_al_2013_Science/section_context_v1_output.json \
  --output-dir miner/ontology_context_v1/outputs/ontology_context_v1/Rietveld_et_al_2013_Science

python -m miner.ontology_context_v1 validate \
  --ontology-output-json miner/ontology_context_v1/outputs/ontology_context_v1/Rietveld_et_al_2013_Science/ontology_context_v1_output.json \
  --output-dir miner/ontology_context_v1/outputs/ontology_context_v1/Rietveld_et_al_2013_Science/validator_contract_report
```
