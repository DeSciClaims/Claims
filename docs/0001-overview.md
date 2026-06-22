# RFC 0001: Overview

## Table of Contents

- [RFC 0001: Overview](./0001-overview.md)
- [RFC 0002: Miner Task](./0002-miner-task.md)
- [RFC 0003: Validator Scoring](./0003-validator-scoring.md)
- [RFC 0004: Schema](./0004-schema.md)
- [RFC 0005: Incentive Mechanism](./0005-incentive-mechanism.md)
- [RFC 0006: Ontology Linking](./0006-ontology-linking.md)
- [RFC 0007: Schema Epistemic Ontology](./0007-schema-epistemic-ontology.md)
- [RFC 0008: Roadmap To Subnet](./0008-roadmap-to-subnet.md)

## Purpose

The Claims subnet is proposed as a market for extracting machine-readable scientific claims from source papers and linking those claims to evidence.

This repository captures the shape of that idea in a way that is easy to inspect:

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
