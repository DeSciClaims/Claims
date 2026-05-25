# RFC 0004: Schema

## Core Objects

This repo uses six simplified schema files.

### `source.schema.json`

Paper-level metadata:

- title
- DOI
- authors
- year

### `chunk.schema.json`

A chunk is a small excerpt from the source paper:

- chunk id
- source id
- section
- text

### `claimframe.schema.json`

An atomic claim with:

- subject
- predicate
- object
- claim type
- epistemic status
- source chunk ids

### `extraction.schema.json`

A miner output containing:

- task metadata
- source
- chunks
- claim records
- optional meta assertions

### `meta_assertion.schema.json`

A higher-level assertion about the extraction output, such as:

- consensus candidate
- contradiction flag
- ontology readiness

### `validator_score.schema.json`

A simple validator report containing:

- total score
- score components
- acceptance decision
- notes

## Guiding Principle

The public schema should show the conceptual shape of the subnet while staying easy to read.
