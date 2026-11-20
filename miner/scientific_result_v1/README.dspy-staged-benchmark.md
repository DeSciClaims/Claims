# Scientific extraction: DSPy reliability fixes

All four supplied papers were processed using source-only Docling data and DSPy Module/Predict programs. Ohtsuki includes its supplied supplementary PDF. Expected worked-example outputs were read only for evaluation, after extraction. This is an iterative development benchmark with resumed source-derived stages, not an equal-work latency or price comparison.

## Changes

- Stage-specific scientific instructions replace the complete final-output workflow in every prompt. Inventory does not generate links to hypothetical result IDs.
- Numerical comparisons are planned separately, and distinct statistics receive labelled IDs rather than opaque row dictionaries.
- Python resolves a terminal comparison core only from existing reviewed evaluated/reference comparison links, with no new edges, no storage inference, no unresolved nodes, and no cyclic or nonterminal anchor. Every correction is auditable.
- Invalid optional absence markers are omitted only when doing so removes validation errors without creating others. Scientific value/ID properties and required evidence are preserved.
- A focused GraphReviewProgram can add or explicitly remove reviewed graph edges and choose existing cores. It cannot rewrite measurements or create scientific IDs. Unsupported attachments remain explicit review items.
- Initial extraction uses no native reasoning; graph review and validation corrections use low reasoning. Requests have a 600-second absolute deadline. Truncated/empty completions are rejected, with bounded retries and durable accounting.

## Corrected DSPy versus worked examples

Each pair is **corrected DSPy / worked example**. Result, field and family counts describe representation, not scientific accuracy or recall. “Result fields” counts fields inside local results; “All fields” also includes shared-context and cross-group-summary fields.

| Paper | Local results | Result fields | All fields | Families |
|---|---:|---:|---:|---:|
| Shukla | 14 / 14 | 84 / 87 | 98 / 119 | 2 / 1 |
| Genther | 33 / 33 | 275 / 215 | 282 / 242 | 3 / 1 |
| Ohtsuki | 38 / 81 | 114 / 115 | 126 / 156 | 5 / 5 |
| Perel | 27 / 56 | 148 / 192 | 153 / 200 | 9 / 6 |

## Numerical tables: corrected DSPy versus worked examples

“Reference cells present” counts matched worked-example data cells, not all generated cells. “Ordered table matches” counts tables matching after typography and empty-layout-row normalization.

| Paper | Tables: DSPy / example | Reference cells present / expected | Ordered table matches / example tables | Declared table coverage: DSPy / example |
|---|---:|---:|---:|---|
| Shukla | 1 / 1 | 56 / 70 | 0 / 1 | complete / complete |
| Genther | 2 / 2 | 129 / 129 | 1 / 2 | complete / complete |
| Ohtsuki | 0 / 0 | 0 / 0 | 0 / 0 | complete / complete |
| Perel | 2 / 2 | 28 / 28 | 1 / 2 | complete / incomplete |

Shukla exports all 70 reference numerical cells: the overlap metric counts 56 measurements and excludes 14 time-condition entries classified as row labels. Genther and Perel retain source layout differences, so full ordered-table equality is not claimed. Ohtsuki has no qualifying numerical source tables; 0 / 0 is not an accuracy score.

**Coverage caveat:** Perel's generated `complete` declaration is incorrect under the guide because the referenced external study-quality table is unavailable. The worked example correctly declares `incomplete`. Exporting both available main-article tables does not resolve that missing-source gap.

The worked examples include digitized plot values, while this pipeline run did not digitize plotted coordinates. Ohtsuki's supplement was supplied locally. These differences limit direct completeness comparisons.

## Development accounting

Recorded provider cost for this development task: $0.8729. This includes controls, failures and resumed development attempts; it is not a per-run price. See session_accounting.json for deduplication and calls with unknown billing.

## Interpretation and limits

Passing schema/reference/graph/CSV checks does not prove semantic fidelity, completeness, correct attachment, or equivalence to the examples. All values and graph decisions remain inspectable. Source plot coordinates were not digitized. Some total-field differences reflect shared-context representation; compare primary result fields separately.

## Reproduce the tested profile

Use the scientific CLI with --reasoning-effort none --retry-reasoning-effort low. Benchmark inputs/settings and source hashes are recorded in benchmark_config.json; cache_lineage.json identifies reused source-only stages. Unique request, response, prediction, normalization, core-projection and graph-review artifacts retain the full history. Session accounting distinguishes development attempts from the final packages and deduplicates copied call IDs.
