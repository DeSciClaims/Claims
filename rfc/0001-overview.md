# RFC 0001: Overview

## Purpose

The Claims subnet is proposed as a market for extracting machine-readable scientific claims from source papers and linking those claims to evidence.

This repository captures the shape of that idea in a way that is easy to inspect:

- a task envelope
- a miner response
- a validator score report
- a minimal ontology-linking layer

## Why This Exists

Scientific AI systems still struggle with structured reasoning over the literature. The missing layer is not just retrieval. It is claim-level structure:

- atomic claims
- evidence linked to source text
- explicit support or contradiction relations
- provenance preserved at the chunk level

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
