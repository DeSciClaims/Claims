# Miner v0

`miner.v0` is the section-context miner shape simplified for the Claims subnet v0. It keeps the existing ingest, section planning, section summaries, exports, and upload-compatible files, but the extraction target is now flat claim-evidence pairs:

- paper-owned `claim_text`
- one or more evidence items per claim
- source span/section provenance

SPO fields, ontology mappings, rich context, and details are compatibility fields only. They should usually be empty.

## What Lives Here

- `runner.py`: end-to-end mining flow
- `__main__.py`: CLI entrypoint
- `config.py`: package-local runtime config
- `dspy_runtime.py`: DSPy/OpenRouter setup for section planning and extraction
- `grobid_client.py` and `tei_parser.py`: ingest helpers
- `prompts/`: prompt files used by the pipeline
- `CLAIM_EXTRACTION_FIELDS.md`: v0 output field reference

## Install

From the `Claims/` directory:

```bash
pip install -r requirements.txt
```

Set up env vars from `.env.example`. The miner needs `OPENROUTER_API_KEY`. For PDF ingestion with TEI extraction it also expects a GROBID endpoint configured by `GROBID_URL`.

## Run

Mine directly from a PDF with GROBID:

```bash
python -m miner.v0 --pdf /path/to/paper.pdf --pdf-extraction-method grobid
```

Mine from a downloadable PDF URL:

```bash
python -m miner.v0 --pdf-url https://example.org/paper.pdf --pdf-extraction-method grobid
```

Mine from a PDF with `pypdf` only:

```bash
python -m miner.v0 --pdf /path/to/paper.pdf --pdf-extraction-method pypdf
```

Mine from an existing TEI XML file:

```bash
python -m miner.v0 --tei-xml /path/to/paper.tei.xml
```

Mine from a prebuilt artifact JSON:

```bash
python -m miner.v0 --artifact-json /path/to/artifact.json
```

Override the output directory:

```bash
python -m miner.v0 --pdf /path/to/paper.pdf --output-dir /tmp/claims-miner-v0
```

## Outputs

Each run writes a paper folder containing:

- `artifact.json`
- `section_context_v1_output.json`
- `extracted_claims.csv`
- `manifest.json`

When GROBID is used, the run also saves `tei.xml`.
