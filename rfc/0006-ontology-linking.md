# RFC 0006: Ontology Linking

## Summary

Claims are more useful when their subject and object fields can be mapped to normalized concepts.

This demo repo includes a minimal hard-coded ontology mapping layer to show the role this step plays:

- normalize repeated entity mentions
- improve mergeability across miners
- support domain-specific downstream views

## Minimal Model

Each semantic field may include:

- raw text
- normalized text
- a small list of candidate mappings
- an optional selected mapping

## Why It Matters

Without normalization, two miners may return semantically identical claims in forms that are hard to align.

Ontology linking is therefore not the whole product, but it is one of the pieces that makes the canonical graph composable.
