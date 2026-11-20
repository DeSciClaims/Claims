# Scientific result extraction v1.1

This pipeline writes the scientific result contract in
`schemas/scientific-result-extraction/v1.1/` directly. It does not convert ARA
artifacts. The existing `agent_v1` production and competition path is separate.

## Run

Install `pip install -e '.[scientific-results]'` in Python 3.11 or newer. Set
`OPENROUTER_API_KEY` or scoped `CLAIMS_INFERENCE_TOKEN` credentials. The default
model is `deepseek/deepseek-v4-flash-0731`, with reasoning disabled for initial extraction and low reasoning for graph review and correction. Requests have a 600-second absolute deadline.

LLM stages use DSPy 3.1.2 `Module`/`Predict` programs with separate typed output
fields for inventory, result batches, source-table audits, families, and repairs.
A custom DSPy adapter preserves the raw baseline's system instructions and JSON
user message exactly, including whitespace and image ordering. It adds no field
wrappers, null instructions, demonstrations, or chain-of-thought output. Each
Predict call makes one JSON-mode request; the canonical JSON Schema and existing
pipeline own domain validation and semantic retries. Parsed keys and values are
preserved without optional-field rewriting. UUID request files retain the
model-facing messages for audit. Runtime version 2 prevents reuse of earlier
DSPy inference caches.

```sh
python -m miner.scientific_result_v1 \
  --pdf paper.pdf --paper-id P001 \
  --model deepseek/deepseek-v4-flash-0731 \
  --output-dir outputs/scientific-results-paper
```

Pass each supplied supplement using `--supplement supplement.pdf`. Use
`--prepare-only` to run only Docling, or `--vision` with a vision-capable model
to include rendered pages. DeepSeek V4 Flash is text-only. `--model` can select a
Qwen model; its modality must match the requested image inputs. Do not assume
text-only parsing recovers graphical coordinates.

Use an empty output directory, or explicitly `--resume` a matching run. Resume
checks source bytes, assigned IDs, schema, model, token limit, reasoning effort,
temperature, batch size, vision setting, and runtime identity. Validated model stages are cached
with request/instruction fingerprints. `--prepared-dir` separately caches Docling
outputs and verifies source hashes.

## Pipeline and output

1. Docling parses the article and supplements using full-page OCR and accurate
   table recognition. Full-page OCR avoids broken publisher font mappings that
   can turn minus and plus/minus signs into digits. It still requires source QA.
2. The model inventories all scientific results, shared context, and numerical
   tables. Every detected candidate is included or explicitly excluded. Page
   continuations are consolidated. Source-display rows, header hierarchy,
   annotations, and transcription qualifications form each table export.
3. Batches extract result fields and reviewed relationships using stable IDs.
   Table coordinates link fields to source cells. Unseen values are not invented.
4. A dedicated source-table audit checks printed numerical regions inside figures,
   beyond the table detector's candidates. Recovered tables and scientific records
   join the extraction before family grouping. Plot axes and curves alone are not
   exported as source tables.
5. Local closure membership and boundary references are computed from reviewed
   relationships and parent-field links, independent of storage and families.
6. The model assigns already derived local results to scientific families. Records
   that summarize several families can move to `cross_group_summaries` while
   preserving their scientific IDs and fields. Table-linked records stay ordinary.
7. Structure, identities, references, family membership, parent paths, closures,
   delivered CSVs, coordinates, and artifact-link provenance are checked.
   Bounded JSON-pointer repairs address deterministic errors without modifying
   source/table identities or deleting scientific content. Invalid runs retain diagnostics and do not publish
   `extraction.json`.

Deliverables are `extraction.json` and one `<table-id>.csv` per numerical table.
Supporting files include `source_payload.json`, `extraction_schema.json`,
`validation_report.json`, `runtime_manifest.json`, and stage diagnostics.
Parser outputs retain `docling_document.json`, Markdown and raw table candidates;
CSV alone cannot retain layout, cell spans and geometry. Model-transcribed CSVs
are distinct from raw Docling candidates and remain auditable in stage files.
Raw `page_ocr.json` and `page_ocr.txt` also retain printed text inside regions
classified as figures, which may be absent from the layout Markdown. Explicit
merged-cell geometry removes DataFrame duplication before extraction; equal
values alone never imply that source cells or scientific tests are shared.

`inference_calls.jsonl` is an append-only ledger of request starts, completions,
and transport errors. Each request and prediction has a UUID so resuming a stage
cannot overwrite its billing history. Provider usage costs and LiteLLM response
costs are distinguished; interrupted requests without usage retain unknown costs.
The DSPy LM has caching and internal retries disabled. The runtime owns at most
two additional transport retries, while the pipeline owns bounded semantic
repairs. A provider `length` finish reason is rejected before adapter parsing.

A valid working extraction can explicitly declare incomplete table/source
coverage. That is not a completed submission. Source fidelity, scientific
attachment, terminal-core judgments, graphical precision, and undiscovered
supplements require review; deterministic validation does not certify them.
The reference runner refuses incomplete table coverage for Bronze delivery.

## Benchmark the worked examples

```sh
python -m miner.scientific_result_v1.benchmark \
  --pdf-dir /path/to/claims-scientific-schema-examples \
  --output-dir outputs/scientific-result-benchmark \
  --prepared-dir outputs/scientific-result-sources \
  --ohtsuki-supplement /path/to/nature04605-appendix.pdf \
  --model deepseek/deepseek-v4-flash-0731
```

Use `--case Shukla` (repeatable) to select cases. Expected JSON/CSV files are read
only after extraction for comparison. Reports compare record/field/family counts,
field labels, numerical leaf overlap and exact normalized nonempty CSV cells.
These are diagnostics, not semantic accuracy or recall: equal values may describe
different scientific quantities. Source scope and digitization also differ.
All four worked examples are regression fixtures for deterministic validation.
`--concurrency` defaults to one paper; `--max-tokens`, `--transport-retries`,
`--max-attempts`, and `--batch-size` make the inference budgets explicit. A fresh
output directory prevents reuse of raw-API inference caches in a DSPy comparison.

Benchmark reports: [original DSPy run](README.dspy-benchmark.md) and
[preserved-prompt rerun](README.dspy-preserved-prompts-benchmark.md).

## Release work remaining

- Benchmark source fidelity and extraction coverage before selecting a release.
- Add a v1.1 downstream validator projection and competition JSON/CSV transport.
- Freeze exact parser dependencies/model assets and build a separate release lock.

The current competition wheel continues to accept only `agent_v1`; v1.1 is an
explicit reference CLI/development output mode, not an automatic worker upgrade.
