# Validator v0

`validator.v0` is the simplified Judge V2 audit flow for miner v0 outputs. It keeps the existing CSV files used by ClaimsReview upload/review tooling, but scores flat claim-evidence pairs instead of rich SPO/context/detail packets.

The validator checks:

- `claim_text` is faithful to the cited source span/section
- the claim is made by this paper, not merely reported from prior work
- each claim has linked evidence item text
- the linked evidence exists in, or is grounded by, the cited source span/section
- the run covers important contribution claims across relevant sections

## Run

From the `Claims/` directory:

```bash
python -m validator.v0 \
  --extraction-output-json miner/v0/outputs/<run>/<paper>/section_context_v1_output.json \
  --audit-method deterministic
```

Use LLM audit mode when you want semantic grounding, prior-work attribution, and evidence-quality judgment:

```bash
python -m validator.v0 \
  --extraction-output-json miner/v0/outputs/<run>/<paper>/section_context_v1_output.json \
  --audit-method llm
```

Write audit outputs somewhere explicit:

```bash
python -m validator.v0 \
  --extraction-output-json miner/v0/outputs/<run>/<paper>/section_context_v1_output.json \
  --audit-method llm \
  --output-dir /tmp/claims-validator-v0
```

## Outputs

- `run_audit_record.csv`
- `claim_audit_records.csv`
- `candidate_missing_claims.csv` when intrinsic LLM coverage discovery runs
- `weak_or_unsupported_claims.csv` when source-support diagnostics are available
- `missing_gold_claims.csv` and `extra_extracted_claims.csv` in gold mode

See `AUDIT_RECORD_FIELDS.md` for the full field reference.
