# Claims Subnet

This repository is the public Claims subnet repo. It includes the docs
material plus self-contained runnable packages for:

- `miner`
- `validator`

Those folders are ongoing work areas. The current public implementation scope is
pipeline-version packages, with `miner/section_context_v1` and
`validator/judge_v1` vendoring the actual benchmark-derived code instead of only
carrying a toy scaffold.

## Repository Layout

```text
claims-subnet-rfc/
├── README.md
├── LICENSE
├── pyproject.toml
├── requirements.txt
├── .env.example
├── docs/
├── schemas/
├── miner/
│   └── section_context_v1/
│       ├── README.md
│       └── requirements.txt
├── validator/
│   └── judge_v1/
│       ├── README.md
│       └── requirements.txt
└── examples/
```

## Core Objects

- `Paper`
- `Span`
- `Claim`
- `EvidenceItem`
- `ClaimEvidenceLink`

The miner produces a paper-level extraction packet built from those objects. The
validator judges that packet either intrinsically or against reviewed gold data.

## Quickstart

Install dependencies from the `Claims/` directory:

```bash
pip install -r requirements.txt
```

Copy `.env.example` to `.env` and fill in `OPENROUTER_API_KEY`. If you want PDF
ingest through TEI, run GROBID and set `GROBID_URL`.

Run the miner on a PDF:

```bash
python -m miner.section_context_v1 --pdf /path/to/paper.pdf
```

Run the validator intrinsically on the miner output:

```bash
python -m validator.judge_v1 \
  --extraction-output-json miner/section_context_v1/outputs/section_context_v1/<paper_id>/section_context_v1_output.json \
  --mode intrinsic
```

Run the validator without the LLM for a local smoke test:

```bash
python -m validator.judge_v1 \
  --extraction-output-json /path/to/section_context_v1_output.json \
  --mode intrinsic \
  --judge-version none
```

## Design Notes

- The public repo keeps the miner and validator packages self-contained.
- The example domain is biomedical, but the shape is domain-agnostic.
- Ontology enhancement uses the same `SemanticField` and `OntologyAnnotation` pattern everywhere.
- `judge_v1` is the first public judge release. It is derived from the benchmark's internal judge-v3 logic, but renamed for the public package.
- Each runnable folder owns its own helpers, prompts, docs, and requirements instead of depending on a shared internal package layer.

## Public Structure

- `miner/` is the public miner root and will grow as more pipeline versions are published.
- `miner/section_context_v1/` is the current self-contained miner pipeline package.
- `validator/` is the public validator root and is likewise currently scoped to packaged judge versions.
- `validator/judge_v1/` is the current self-contained validator judge package.

## Suggested Reading Order

1. `docs/README.md`
2. `docs/0001-miner-task.md`
3. `docs/0002-validator-scoring.md`
4. `docs/0003-schema.md`
5. `miner/section_context_v1/runner.py`
6. `validator/judge_v1/runner.py`

## Status

This repository is still documentation-first. The miner and validator folders
now contain runnable benchmark-derived code, but they should still be read as
ongoing pipeline-version work rather than a finished top-level framework.
