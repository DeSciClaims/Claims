# Scientific Claims Subnet RFC

This repository is a lightweight, public-facing guide to the proposed Claims subnet.

It is intentionally not a production codebase. The goal is to make the shape of the subnet easy to understand for reviewers, partners, and potential miners or validators:

- What a source-paper task looks like
- What a miner is expected to return
- How a validator might score that output
- How claims, evidence, and ontology links fit together

The examples and scripts are deliberately minimal and hard-coded. They are meant to illustrate protocol shape, not implementation detail.

## What Is In This Repo

- Short RFC notes describing the proposed subnet
- Simplified JSON Schemas for source, chunk, claim, extraction, and validator outputs
- Mock miner and validator scripts that run on example JSON files
- Example payloads for valid and invalid miner outputs
- Small tests that validate the repo's demonstration flow

## What Is Not In This Repo

- Full extraction pipelines
- Production networking or storage layers
- Model-serving infrastructure
- Customer-specific ontology systems
- Economic mechanism implementation beyond a simple sketch

## Repository Layout

```text
claims-subnet-rfc/
├── README.md
├── LICENSE
├── pyproject.toml
├── requirements.txt
├── .env.example
├── rfc/
├── schemas/
├── simulator/
├── examples/
├── tests/
└── scripts/
```

## Core Idea

The subnet treats a scientific paper as input and a set of structured `ClaimRecord` objects as output.

Each `ClaimRecord` has:

- a `claim`:
  an atomic subject-predicate-object statement with provenance
- one or more `evidence` items:
  structured support, contradiction, qualification, or replication records linked back to source chunks
- optional `meta_assertions`:
  higher-level statements such as consensus status, conflict flags, or ontology readiness

Validators score miner outputs for shape, grounding, and basic consistency, then use accepted records to help build a canonical claim-evidence graph.

## Quickstart

Run the end-to-end demo:

```bash
python scripts/run_demo.py
```

Validate an example payload:

```bash
python scripts/validate_payload.py examples/miner_outputs/extraction.valid.json schemas/extraction.schema.json
```

Score an example miner output:

```bash
python scripts/score_payload.py examples/miner_outputs/extraction.valid.json
```

Run tests:

```bash
python -m unittest discover -s tests
```

## Design Notes

- The schema is simplified from the internal prototype used by the team.
- The example domain is biomedical, but the shape is domain-agnostic.
- Ontology linking is shown as a simple mapping layer, not as a full service.

## Suggested Reading Order

1. `rfc/0001-overview.md`
2. `rfc/0002-miner-task.md`
3. `rfc/0003-validator-scoring.md`
4. `rfc/0004-schema.md`
5. `scripts/run_demo.py`

## Status

This repository is a communication artifact for early review. It should be read as an RFC and demo scaffold, not as a final subnet spec.
