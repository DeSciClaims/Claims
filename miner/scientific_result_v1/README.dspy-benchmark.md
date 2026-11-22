# DSPy scientific extraction benchmark

> Historical first DSPy run. See the [corrected adapter and raw-input rerun](README.dspy-preserved-prompts-benchmark.md).

All four PDFs were processed with DSPy 3.1.2 `Module`/`Predict` stage programs and DeepSeek V4 Flash 0731. Model settings: temperature 0, low native reasoning effort, 32,768 completion tokens, result batch size 4, maximum two concurrent papers. No DSPy optimizer or gold examples were used for inference.

Each model stage has separate typed output fields. There is no ReAct loop or extra generated rationale field. The JSON adapter makes one JSON-mode request; DSPy/LiteLLM retries and response caching are disabled. The runtime owns bounded transport retries; the pipeline owns schema/semantic repairs.

## Outputs versus raw-API baseline and worked examples

| Paper | Results: raw / DSPy / example | Fields: raw / DSPy / example | Families: raw / DSPy / example | Tables: DSPy / example | Reference data cells present |
|---|---:|---:|---:|---:|---:|
| Shukla | 14 / 4 / 14 | 103 / 23 / 119 | 1 / 1 / 1 | 1 / 1 | 70 / 70 |
| Genther | 6 / 6 / 33 | 79 / 74 / 242 | 2 / 2 / 1 | 2 / 2 | 129 / 129 |
| Ohtsuki | 39 / 11 / 81 | 150 / 50 / 156 | 6 / 6 / 5 | 0 / 0 | 0 / 0 |
| Perel | 21 / 33 / 56 | 180 / 114 / 200 | 7 / 7 / 6 | 2 / 2 | 28 / 28 |

## Calls, cost, and failures

| Paper | API requests | Truncated | HTTP 429 | Provider-reported USD |
|---|---:|---:|---:|---:|
| Shukla | 7 | 1 | 0 | $0.038665 |
| Genther | 6 | 0 | 0 | $0.029834 |
| Ohtsuki | 10 | 3 | 0 | $0.066875 |
| Perel | 14 | 1 | 0 | $0.072947 |
| **Total** | **37** | **5** | **0** | **$0.208320** |

Input tokens: 1,359,288. Output tokens: 461,426, including 340,056 native reasoning tokens.
Observed inference wall time was about 20.6 minutes. Truncated attempts accounted for $0.067067 of the total.

Unfinished requests: 0; requests without recorded cost: 0. Costs come from provider usage metadata and include retries. The separate connectivity smoke test is excluded.

## Interpretation

All five table exports have equal ordered data rows to their references after typography normalization and removal of empty layout rows.

All final packages pass the implemented schema, reference, graph, and CSV checks. That is structural validity, not a certificate of scientific completeness or correct attachments. Results and fields can represent collections, so count differences do not directly measure recall.
The raw baseline was an iterative development run with cached/refined outputs, variable concurrency and discarded attempts. This DSPy run used consistent current parser options with retained page OCR and explicit concurrency. Therefore cost or coverage differences are not a controlled estimate of the effect of DSPy alone.
The previous raw development logs recorded at least $0.427754 across 73 completed calls, including a Qwen probe and development retries. The fresh DSPy totals above should not be compared as an equal-work production price benchmark.
Truncation is checked on the provider response before DSPy JSON parsing, so a repaired partial JSON response cannot be silently accepted. UUID call records and the append-only inference_calls.jsonl ledger prevent overwritten usage during resume. Schema retries and transport errors are reported separately.
The DSPy output uses fewer, larger collections for several papers. It does not match the reference granularity or all independently addressable scientific objects. No graph digitization was performed by the text-only model. Review the numerical, family-granularity, and source-attachment differences in each comparison JSON. Perel may remain incomplete because the externally identified source table was not supplied.

A boolean JSON Schema `items=false` signature edge case was fixed before Ohtsuki made its first API call. Its corrected run used the same model settings and paper inputs; `initial_pass_summary.json` preserves the initial coordinator result.

Validation: 36 pipeline tests and 14 reference-miner tests passed.

## Files

Each paper folder contains extraction.json, table CSVs, source_payload.json, extraction_schema.json, validation_report.json, runtime_manifest.json, comparison.json, inference_calls.jsonl, and detailed stage records. benchmark_config.json freezes the generation and concurrency settings. accounting_summary.json contains machine-readable totals.
