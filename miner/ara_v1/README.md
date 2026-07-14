# ARA V1 Miner

`ara_v1` is a self-contained ARA compiler for papers. It ingests a PDF, text file,
or extraction artifact JSON, calls its own ARA-specific LLM prompt, validates the
structured result, and writes an Agent-Native Research Artifact layout.

It writes both:

- `ara_v1_output.json`: canonical structured artifact payload.
- A markdown ARA tree: `PAPER.md`, `logic/`, `evidence/`, `trace/`, `src/`.

## Run

```bash
python -m miner.ara_v1 \
  --pdf /path/to/paper.pdf \
  --output-dir /tmp/paper_ara
```

Alternative inputs:

```bash
python -m miner.ara_v1 --artifact-json /path/to/artifact.json --output-dir /tmp/paper_ara
python -m miner.ara_v1 --text /path/to/paper.txt --output-dir /tmp/paper_ara
```

## Environment

`ara_v1` uses DSPy/OpenRouter and requires `OPENROUTER_API_KEY`.

Optional environment variables:

- `SUBNET_CLAIMS_ARA_OPENROUTER_MODEL`
- `SUBNET_CLAIMS_ARA_TEMPERATURE`
- `SUBNET_CLAIMS_ARA_MAX_TOKENS`
- `SUBNET_CLAIMS_ARA_MAX_SOURCE_CHARS`

## Scope

This implementation does not depend on `section_context_v1` output. It keeps the
structured JSON as the source of truth and renders markdown from that JSON.

The first visual evidence pass preserves source text and source files, but does
not yet crop figure/table screenshots from PDFs.
