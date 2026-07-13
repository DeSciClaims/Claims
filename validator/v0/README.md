# Validator v0

`validator.v0` is the simplified Judge V2 audit flow for miner v0 outputs. It keeps the existing CSV files used by ClaimsReview upload/review tooling, but scores flat claim-evidence pairs instead of rich SPO/context/detail packets.

The validator checks:

- the extracted claim and linked evidence item text exist in, or are grounded by, the cited source span/section
- each claim row has linked evidence items before link validity is scored
- each claim-evidence link is valid and relevant for the given claim
- the run covers the claims in its extraction scope

The CSV column names remain backward-compatible with existing ClaimsReview imports:

- `accurate_extraction_score` now means source existence/grounding for the claim and linked evidence.
- `evidence_evaluation_score` now means claim-evidence link validity.
- `complete_coverage_score` remains run-level and is extraction-mode aware. For `abstract-full-paper`, coverage targets abstract claims and uses the full paper as evidence context. For section-local/full-text modes, coverage targets important claims across relevant sections.

For `abstract-full-paper`, LLM missing-claim discovery is hard-scoped to the abstract text/spans. The validator does not pass body sections or the full-paper summary into the coverage-discovery payload, and it filters out any returned candidate whose `source_span_ids` are outside the abstract scope.

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
- `weak_or_unsupported_claims.csv` when link-validity diagnostics are available
- `missing_gold_claims.csv` and `extra_extracted_claims.csv` in gold mode

See `AUDIT_RECORD_FIELDS.md` for the full field reference.
