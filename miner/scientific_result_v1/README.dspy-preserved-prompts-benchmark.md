# DSPy benchmark with preserved raw prompts

Runtime version 2 uses DSPy 3.1.2 Module/Predict programs with a custom adapter. It sends the original system instruction and JSON user payload without DSPy signature scaffolding, field-format instructions, whitespace normalization, or optional-null rewriting. No extraction prompts, result planning, batching, grouping, or repair rules were changed for this rerun.

An offline comparison executed the archived raw API runtime and current DSPy runtime and verified identical messages, temperature, max_tokens, reasoning settings, response format, and parsed output. Each live call saves its model-facing request with a UUID for audit. All four reconstructed source payloads were verified equal to the original raw benchmark source payloads before inference. See implementation_provenance.json for source hashes.

All four papers used fresh inference with source-only Docling inputs verified equal to the raw baseline source payloads, including supplied Ohtsuki supplementary material. Model: deepseek/deepseek-v4-flash-0731; temperature 0; low native reasoning effort; completion budget 32768; batch size 4; maximum 3 semantic attempts per stage and 2 additional transport retries; paper concurrency 2. Worked examples were read only after extraction for comparison.

**Outcome: 2/4 structurally valid packages. Output-quality parity with the historical raw baseline is not established.**

## Counts

Cells show raw baseline / previous DSPy / preserved-prompt DSPy / worked example. Counts describe representation, not scientific recall.

| Paper | Results | Fields | Families | New status |
|---|---:|---:|---:|---|
| Shukla | 14 / 4 / — / 14 | 103 / 23 / — / 119 | 1 / 1 / — / 1 | failed |
| Genther | 6 / 6 / 7 / 33 | 79 / 74 / 140 / 242 | 2 / 2 / 2 / 1 | valid_working_extraction |
| Ohtsuki | 39 / 11 / — / 81 | 150 / 50 / — / 156 | 6 / 6 / — / 5 | failed |
| Perel | 21 / 33 / 27 / 56 | 180 / 114 / 141 / 200 | 7 / 7 / 7 / 6 | valid_working_extraction |

## Cost and failures

| Paper | Completed calls | Truncated | HTTP 429 | Recorded USD |
|---|---:|---:|---:|---:|
| Shukla | 5 | 4 | 0 | $0.053556 |
| Genther | 5 | 0 | 0 | $0.050788 |
| Ohtsuki | 3 | 3 | 0 | $0.042797 |
| Perel | 14 | 4 | 0 | $0.141417 |
| Total | 27 | 11 | 0 | $0.288558 |

Input tokens: 942,030; output tokens: 656,072; native reasoning tokens: 502,991. Unfinished requests: 0; requests without cost: 0. All recorded attempts, including retries, are included. Local PDF processing and development costs are excluded.

## Table and numerical diagnostics

| Paper | Reference data cells present | Ordered normalized table matches | Matched numerical leaves, excluding digitized |
|---|---:|---:|---:|
| Shukla | No extraction | — | — |
| Genther | 129 / 129 | 1 / 2 | 146 / 263 |
| Ohtsuki | No extraction | — | — |
| Perel | 28 / 28 | 1 / 2 | 102 / 173 |

## Failed cases

- Shukla: results-001 failed after 3 attempts; see stage feedback files. See retained stage feedback and raw responses.
- Ohtsuki: inventory failed after 3 attempts; see stage feedback files. See retained stage feedback and raw responses.

## Limits

The earlier stopped diagnostic run used extra page OCR and is excluded from the table above. Its accounting is retained in implementation_provenance.json; interrupted requests may have unknown costs. That run must not be interpreted as the requested raw-input parity comparison. The stopped diagnostic run recorded 19 completed requests costing $0.198616, plus two interrupted requests with unreported costs. Combined recorded cost for this work is therefore at least $0.487174; see session_accounting.json.
Historical raw per-paper snapshots span pipeline revisions. In particular, the retained Shukla inventory request included extra unused schema definitions that the existing current pipeline prunes. Genther’s initial request matches after restoring source inputs; the final Ohtsuki/Perel source-audit directories do not retain initial requests. This rerun changes the adapter and restores source inputs, without undoing existing pipeline development. The historical raw output was built through iterative development and refinement. Preserving its request construction does not reproduce those earlier development steps or guarantee identical stochastic model output. This rerun tests the corrected DSPy integration; it is not an identical-treatment replay of the historical raw benchmark.
Schema, reference, graph, and CSV validation checks structure. They do not prove scientific completeness, correct attachments, or equivalence to the worked examples. Numerical overlap can be affected by strings versus numbers, repeated values, collection representation, and source scope. No graph coordinates were digitized by this text-only model. Perel may declare incomplete table coverage when an externally referenced table is unavailable.

## Artifacts

Case directories retain extraction.json when valid, working_extraction.json when available, source payloads, table CSVs, validation diagnostics, runtime manifest, comparison.json, inference_calls.jsonl and stages/*.request.json / *.response.txt / *.usage.json. benchmark_config.json records settings, implementation_provenance.json records code hashes, and accounting_summary.json records totals.


## Code verification

39 Claims pipeline tests and 14 reference-miner tests passed (53 total). The tests include exact message formatting, multimodal ordering, unchanged parsed keys/nulls, no hidden adapter fallback, and the serialized HTTP generation contract. Selected lint and whitespace checks passed.
