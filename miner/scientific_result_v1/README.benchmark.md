# Raw-API baseline: scientific result extraction v1.1

Historical baseline before the DSPy migration. See [DSPy benchmark](README.dspy-benchmark.md) for the current rerun.

Model: `deepseek/deepseek-v4-flash-0731`, low reasoning effort, temperature 0. Docling 2.151.0 used full-page OCR and accurate table recognition. All four supplied PDFs were tested; Ohtsuki includes the archived supplement.

This is an iterative development benchmark. Source parsing, table normalization, grouping, and repair were corrected during testing. Perel and Ohtsuki were finalized from cached source-derived extraction using the new audit/finalizer. The worked-example JSON and CSV contents were never extraction inputs.

| Paper | Local results: generated / example | Fields: generated / example | Families: generated / example | Tables: generated / example | Table data cells present / expected |
|---|---:|---:|---:|---:|---:|
| Shukla | 14 / 14 | 103 / 119 | 1 / 1 | 1 / 1 | 70 / 70 |
| Genther | 6 / 33 | 79 / 242 | 2 / 1 | 2 / 2 | 129 / 129 |
| Ohtsuki | 39 / 81 | 150 / 156 | 6 / 5 | 0 / 0 | 0 / 0 |
| Perel | 21 / 56 | 180 / 200 | 7 / 6 | 2 / 2 | 28 / 28 |

All four final JSON/CSV packages pass the implemented deterministic schema, ID/reference, family, parent-path, closure, and file checks. No final local closures are marked review_required. These checks do not certify source completeness or scientific interpretation. Perel correctly retains incomplete external-table coverage and is not a completed submission.

## Findings

- Full-page OCR fixed publisher font mappings that initially converted minus signs and plus/minus signs into digits in Shukla.
- Explicit merged-cell geometry removed six duplicated joint-test p-values introduced by the Genther DataFrame export. All 129 reference data cells are present once. Its first table still differs in layout and placement of some joint p-values; category headings add CSV rows. This is a source-attachment review item.
- A dedicated source-table audit recovered Perel's five-row printed table within Figure 1, in addition to its six-row study-quality table.
- Shukla and both Perel tables have equal ordered data rows after typography normalization and removal of empty layout rows. Genther Table 2 also passes that ordered comparison. Genther Table 1 does not.
- Ohtsuki correctly has no numerical source tables. The text-only extraction does not reproduce the reference's digitized curves. It also stores many mathematical values as expressions, so numeric-leaf counts understate the retained textual mathematics.
- Result, field, and family counts differ substantially. Collections versus individual records account for some differences; these counts and numeric overlap are diagnostics, not semantic recall or accuracy.
- A Qwen vision inventory probe also missed the printed forest-plot table; that exploratory full run was stopped. The final four packages reported here use DeepSeek. The separate numerical-region audit was required.

## Status

The pipeline is usable for extraction and review. Reference parity remains incomplete, especially graph digitization, independently addressable scientific detail, family boundaries, and joint-test attachment. Scientific/source review remains necessary before production use. Competition transport and downstream validator projection are separate follow-on work.

Local benchmark comparison JSONs retain field-label/origin counts, numeric diagnostics, table-cell differences, ordered data comparisons, source-coverage declarations, and package hashes. Diagnostic stage files remain under the temporary benchmark directory.

Validation: 28 pipeline tests and 14 reference-miner tests passed. The Claims dependency lock check, wheel schema-resource check, and whitespace checks also passed.
