# Validator Scoring

## Summary

Validators score miner outputs for basic quality before using them in a canonical claim graph.

This demo repo uses a very simple scoring sketch:

- schema validity
- structural completeness
- grounding to known span ids
- role and relation-label validity
- ontology-link completeness

## Demo Scoring Principle

The validator should reward outputs that are:

- well-formed
- grounded in the supplied spans
- semantically legible
- easy to merge with other miner outputs

## What This Repo Does Not Specify

- the final subnet incentive mechanism
- weight setting
- anti-copying strategy
- gold-set operations
- consensus economics

Those belong in a later mechanism specification.
