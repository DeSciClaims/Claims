# RFC 0002: Miner Task

## Summary

A validator sends a miner a paper-level extraction task.

The miner returns a structured extraction artifact that includes:

- one or more `Span` objects
- one or more `Claim` objects
- one or more `EvidenceItem` objects
- one or more `ClaimEvidenceLink` objects

## Minimal Task Envelope

```json
{
  "task_id": "task-hanlon-001",
  "task_family": "claim_extraction",
  "schema_version": "0.1",
  "paper_id": "paper_hanlon_2025_jama",
  "expected_output": "extraction"
}
```

## Minimal Miner Responsibilities

1. Read the paper and span text.
2. Extract one or more atomic `Claim` objects.
3. Extract one or more `EvidenceItem` objects grounded in the provided `Span` objects.
4. Link claims to evidence with `ClaimEvidenceLink`.
5. When possible, assign a `claim_profile` to each claim so expected
   `context` and `details` fields are easier to validate downstream.
6. Return structured JSON conforming to the schema.

## Non-Goals In This Demo Repo

- model selection
- prompt design
- parsing pipelines
- document ingestion complexity

Those choices are intentionally left open for real miners.
