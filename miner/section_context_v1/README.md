# Section Context V1 Miner

This folder contains the real `section_context_v1` miner flow, vendored into a
self-contained public package.

## What Lives Here

- `runner.py`: end-to-end mining flow
- `__main__.py`: CLI entrypoint
- `config.py`: package-local runtime config
- `dspy_runtime.py`: DSPy/OpenRouter setup for section planning and extraction
- `grobid_client.py` and `tei_parser.py`: ingest helpers
- `prompts/`: prompt files used by the pipeline
- `README.benchmark.md`: the original benchmark design note kept for reference

## Install

From the `Claims/` directory:

```bash
pip install -r requirements.txt
```

Set up env vars from `.env.example`. The miner needs `OPENROUTER_API_KEY`. For
PDF ingestion with TEI extraction it also expects a running GROBID instance at
`GROBID_URL`.

Official GROBID setup guide:
https://grobid.readthedocs.io/en/latest/Grobid-docker/

## Run

Mine directly from a PDF with GROBID:

```bash
python -m miner.section_context_v1 --pdf /path/to/paper.pdf
```

Mine from a PDF with `pypdf` only:

```bash
python -m miner.section_context_v1 --pdf /path/to/paper.pdf --pdf-extraction-method pypdf
```

Mine from an existing TEI XML file:

```bash
python -m miner.section_context_v1 --tei-xml /path/to/paper.tei.xml
```

Mine from a prebuilt artifact JSON:

```bash
python -m miner.section_context_v1 --artifact-json /path/to/artifact.json
```

Override the output directory if you do not want the default package-local
`miner/section_context_v1/outputs/...` location:

```bash
python -m miner.section_context_v1 --pdf /path/to/paper.pdf --output-dir /tmp/claims-run
```

## Outputs

Each run writes a paper folder containing:

- `artifact.json`
- `section_context_v1_output.json`
- `extracted_claims.csv`
- `manifest.json`

When GROBID is used, the run also saves `tei.xml`.
