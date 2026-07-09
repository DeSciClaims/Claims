# Claims Subnet

This repository is the public Claims subnet repo. It includes the docs
material plus self-contained runnable packages for:

- `miner`
- `validator`
- `neurons`

Those folders are ongoing work areas. The current public implementation scope is
pipeline-version packages. `miner/v0` and `validator/v0` are the first flat
localnet-ready loop, while `miner/section_context_v1` and `validator/judge_v1`
carry the richer benchmark-derived claim schema work.

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
│   ├── v0/
│   │   ├── README.md
│   │   └── CLAIM_EXTRACTION_FIELDS.md
│   └── section_context_v1/
│       ├── README.md
│       └── requirements.txt
├── validator/
│   ├── v0/
│   │   ├── README.md
│   │   └── AUDIT_RECORD_FIELDS.md
│   └── judge_v1/
│       ├── README.md
│       └── requirements.txt
├── neurons/
│   ├── miner.py
│   ├── validator.py
│   └── protocol.py
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
ingest through TEI, run GROBID and set `GROBID_URL`. Official setup guide:
https://grobid.readthedocs.io/en/latest/Grobid-docker/

Run the flat v0 miner on a PDF:

```bash
python -m miner.v0 --pdf /path/to/paper.pdf
```

Run the flat v0 validator:

```bash
python -m validator.v0 \
  --extraction-output-json miner/v0/outputs/section_context_v1__run_<label>/<paper_id>/section_context_v1_output.json
```

Run the v0 Bittensor miner and validator on localnet:

```bash
python -m neurons.miner --netuid 2 --wallet.name test-miner --wallet.hotkey default --subtensor.chain_endpoint ws://127.0.0.1:9945

python -m neurons.validator \
  --netuid 2 \
  --wallet.name test-validator \
  --wallet.hotkey default \
  --subtensor.chain_endpoint ws://127.0.0.1:9945 \
  --claims.task-artifact miner/v0/outputs/section_context_v1__run_claims_v0/<paper_id>/artifact.json
```

See `docs/0010-bittensor-localnet.md` for the localnet setup, wallet funding,
subnet registration, and neuron runbook.

Run the richer section-context miner on a PDF:

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
- `miner/v0/` is the first flat claim-evidence miner for localnet testing.
- `miner/section_context_v1/` is the richer self-contained miner pipeline package.
- `validator/` is the public validator root and is likewise currently scoped to packaged judge versions.
- `validator/v0/` is the first deterministic flat claim-evidence validator.
- `validator/judge_v1/` is the richer self-contained validator judge package.

## Suggested Reading Order

1. `docs/README.md`
2. `docs/0001-miner-task.md`
3. `docs/0002-validator-scoring.md`
4. `docs/0003-schema.md`
5. `docs/0009-v0-miner-validator.md`
6. `docs/0010-bittensor-localnet.md`
7. `neurons/README.md`
8. `miner/v0/runner.py`
9. `validator/v0/runner.py`
10. `miner/section_context_v1/runner.py`
11. `validator/judge_v1/runner.py`

## Contributing

See [CONTRIBUTING.md](./CONTRIBUTING.md)
for the expected workflow and the current pipeline-scoped contribution model.

## Status

This repository is still documentation-first. The miner and validator folders
now contain runnable benchmark-derived code, but they should still be read as
ongoing pipeline-version work rather than a finished top-level framework.
