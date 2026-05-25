# RFC 0002: Miner Task

## Summary

A validator sends a miner a source document task.

The miner returns a structured extraction payload that includes:

- the source metadata
- one or more source chunks
- one or more `ClaimRecord` objects
- optional meta assertions

## Minimal Task Envelope

```json
{
  "task_id": "task-hanlon-001",
  "task_family": "claim_extraction",
  "schema_version": "0.1",
  "source_id": "hanlon-2025-jama",
  "expected_output": "extraction"
}
```

## Minimal Miner Responsibilities

1. Read the source and chunk text.
2. Extract one or more atomic claims.
3. Link each claim to evidence grounded in the provided chunks.
4. Return structured JSON conforming to the schema.

## Non-Goals In This Demo Repo

- model selection
- prompt design
- parsing pipelines
- document ingestion complexity

Those choices are intentionally left open for real miners.
