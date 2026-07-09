# Docs Overview

## Table of Contents

- [Docs Overview](./README.md)
- [Miner Task](./0001-miner-task.md)
- [Validator Scoring](./0002-validator-scoring.md)
- [Schema](./0003-schema.md)
- [Incentive Mechanism](./0004-incentive-mechanism.md)
- [Ontology Linking](./0005-ontology-linking.md)
- [Schema Epistemic Ontology](./0006-schema-epistemic-ontology.md)
- [Implementation Roadmap](./0007-roadmap-to-subnet.md)
- [Extraction Audit Flow](./0008-extraction-audit-flow.md)
- [Miner v0 and Validator v0](./0009-v0-miner-validator.md)
- [Bittensor Localnet Operation](./0010-bittensor-localnet.md)

## Purpose

The Claims subnet is a market for extracting machine-readable scientific claims
from source papers and linking those claims to evidence.

This repository implements that loop with inspectable schemas, examples, and
runnable miner and validator packages:

- a task envelope
- a miner response bundle
- a validator score report
- a minimal ontology-enhancement layer

## Why This Exists

Scientific AI systems still struggle with structured reasoning over the literature. The missing layer is not just retrieval. It is claim-level structure:

- atomic claims
- evidence linked to source spans
- explicit support or contradiction relations
- provenance preserved at the span level

## Canonical Terms

This repo uses the canonical object names defined across the public schemas in `schemas/`:

- `Paper`
- `Span`
- `Claim`
- `EvidenceItem`
- `ClaimEvidenceLink`
- `SemanticField`
- `OntologyAnnotation`
- `miner.v0` uses a flat claim-evidence payload for the first localnet loop.

## Repo Philosophy

This repo is intentionally small and concrete.

- It favors examples over abstractions.
- It favors readability over completeness.
- It favors hard-coded templates over hidden machinery.

## Intended Audience

- Bittensor ecosystem reviewers
- Potential subnet collaborators
- Prospective miners and validators
- Technical partners evaluating the idea
