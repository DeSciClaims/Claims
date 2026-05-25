# RFC 0004: Schema

## Core Objects

This repo uses the canonical object names from the prototype claim graph schema.

### `paper.schema.json`

`Paper` is the source document object.

### `span.schema.json`

`Span` is the provenance anchor object.

### `claim.schema.json`

`Claim` is the paper-specific assertion with a queryable subject-predicate-object core plus epistemic metadata.

### `evidence_item.schema.json`

`EvidenceItem` is the first-class evidence object linked to source spans.

### `claim_evidence_link.schema.json`

`ClaimEvidenceLink` is the explicit relation between a `Claim` and an `EvidenceItem`.

### `extraction.schema.json`

This is the transport bundle used in the demo repo. It groups:

- one `Paper`
- zero or more `Span` objects
- zero or more `Claim` objects
- zero or more `EvidenceItem` objects
- zero or more `ClaimEvidenceLink` objects

### `validator_score.schema.json`

A simple validator report containing:

- a paper identifier
- a total score
- score components
- an acceptance decision
- notes

## Guiding Principle

The public schema should use the prototype claim graph vocabulary consistently and avoid parallel naming systems.
