# Miner v0 and Validator v0

## Purpose

`miner.v0` and `validator.v0` are the first subnet-shaped implementations for localnet work.

The goal is to get the core loop running before adding richer schemas:

1. miner extracts paper-owned claim-evidence pairs
2. validator scores those pairs
3. outputs upload into ClaimsReview with the existing import scripts

## Miner v0

The miner extracts flat claim-evidence pairs:

- `claim_text`
- `evidence_text`
- source span IDs
- section metadata inherited from the parsed paper

It intentionally does not emit SPO triples, ontology mappings, rich context, or detailed payloads. Compatibility placeholder fields are still present in `extracted_claims.csv` because the current review database has those columns.

### Claim Boundary

The miner should extract claims made by the paper itself:

- findings
- core methods
- conclusions
- stated contributions
- results the paper reports

It should not extract claims that are only made by other papers in background or related-work sections.

Every claim must have a linked evidence item.

## Validator v0

The validator checks:

- claim text appears or is strongly supported in the paper section/span
- evidence text appears or is strongly supported in the paper section/span
- the claim has at least one linked evidence item
- the claim is likely this paper's own claim, not only prior-work background
- the run covers more relevant sections when possible

Scoring is deliberately simple and expected to change.

## Review Compatibility

Miner v0 writes:

- `artifact.json`
- `section_context_v1_output.json`
- `miner_v0_output.json`
- `extracted_claims.csv`
- `manifest.json`

Validator v0 writes:

- `claim_audit_records.csv`
- `run_audit_record.csv`
- `claim_evidence_pair_checks.csv`
- `manifest.json`

The `section_context_v1_output.json` filename is retained so the ClaimsReview scripts can import v0 output without another adapter.

## Commands

Run miner:

```bash
cd Claims
SUBNET_CLAIMS_RUN_LABEL=claims_v0 python -m miner.v0 --pdf /path/to/paper.pdf --pdf-extraction-method grobid
```

Run validator:

```bash
python -m validator.v0 \
  --extraction-output-json miner/v0/outputs/section_context_v1__run_claims_v0/<paper_id>/section_context_v1_output.json \
  --extraction-run-id claims_v0
```

Run validator with LLM semantic audit:

```bash
python -m validator.v0 \
  --extraction-output-json miner/v0/outputs/section_context_v1__run_claims_v0/<paper_id>/section_context_v1_output.json \
  --extraction-run-id claims_v0 \
  --audit-method llm
```

Upload to ClaimsReview:

```bash
cd ../ClaimsReviews
npm run import:claims -- ../Claims/miner/v0/outputs/section_context_v1__run_claims_v0/<paper_id>/extracted_claims.csv --reviewer-id reviewer_204953859 --run-id claims_v0
npm run import:audits -- ../Claims/miner/v0/outputs/section_context_v1__run_claims_v0/<paper_id>/judge_intrinsic_audit_v2/claim_audit_records.csv
npm run import:run-audits -- ../Claims/miner/v0/outputs/section_context_v1__run_claims_v0/<paper_id>/judge_intrinsic_audit_v2/run_audit_record.csv
```
