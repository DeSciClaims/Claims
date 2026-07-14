# Claims Subnet

Claims is a Bittensor subnet for extracting scientific contribution claims from
papers and linking each claim to grounded evidence in the source text.

This repository contains the runnable miner, validator, protocol, schemas, and
operator documentation for the Claims subnet.

## What It Does

- Miners receive paper-extraction tasks and return structured claim-evidence
  packets.
- Validators audit miner outputs for source grounding, valid claim-evidence
  links, and coverage of the task scope.
- The neuron entry points expose the miner and validator through Bittensor.

The current default pipeline is `v0`: abstract contribution claims linked to
evidence from the full paper.

## Repository Layout

```text
Claims/
├── miner/v0/          # claim extraction pipeline
├── validator/v0/      # audit and scoring pipeline
├── neurons/           # Bittensor miner, validator, and protocol
├── schemas/           # shared data contracts
├── docs/              # design notes and operator runbooks
├── examples/          # example papers and inputs
├── requirements.txt
└── .env.example
```

## Install

From the repository root:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip setuptools wheel
python -m pip install -r requirements.txt
```

Create your environment file:

```bash
cp .env.example .env
```

Set at least:

```bash
OPENROUTER_API_KEY=...
GROBID_URL=https://your-grobid-host
```

`GROBID_URL` is required for PDF-to-TEI extraction with `--pdf-extraction-method grobid`.

## Run The Miner Locally

Extract claims from a PDF:

```bash
python -m miner.v0 \
  --pdf /path/to/paper.pdf \
  --pdf-extraction-method grobid \
  --mode abstract-full-paper \
  --output-dir miner/v0/outputs/my_run
```

Extract from an existing TEI XML file:

```bash
python -m miner.v0 \
  --tei-xml /path/to/tei.xml \
  --mode abstract-full-paper \
  --output-dir miner/v0/outputs/my_run
```

The main miner output is:

```text
miner/v0/outputs/<run>/<paper_id>/section_context_v1_output.json
```

The reviewer/import-friendly CSV is:

```text
miner/v0/outputs/<run>/<paper_id>/extracted_claims.csv
```

## Run The Validator Locally

Run the LLM audit:

```bash
python -m validator.v0 \
  --extraction-output-json miner/v0/outputs/<run>/<paper_id>/section_context_v1_output.json \
  --audit-method llm \
  --output-dir validator/v0/outputs/<run>
```

Validator outputs include:

```text
run_audit_record.csv
claim_audit_records.csv
candidate_missing_claims.csv
weak_or_unsupported_claims.csv
```

## Run A Bittensor Miner

Start a miner neuron after the wallet hotkey is registered on the target subnet:

```bash
python -m neurons.miner \
  --netuid <NETUID> \
  --wallet.name <MINER_WALLET> \
  --wallet.hotkey <HOTKEY> \
  --subtensor.network <NETWORK> \
  --axon.ip 0.0.0.0 \
  --axon.external_ip <PUBLIC_IP> \
  --axon.port 8091 \
  --axon.external_port 8091 \
  --claims.extraction-mode abstract-full-paper \
  --claims.pdf-extraction-method grobid \
  --claims.output-dir miner/v0/outputs/neuron
```

Use `--subtensor.chain_endpoint <WS_ENDPOINT>` instead of
`--subtensor.network <NETWORK>` when connecting to a custom chain endpoint.

## Run A Bittensor Validator

Start a validator neuron after the validator hotkey is registered and ready to
submit weights:

```bash
python -m neurons.validator \
  --netuid <NETUID> \
  --wallet.name <VALIDATOR_WALLET> \
  --wallet.hotkey <HOTKEY> \
  --subtensor.network <NETWORK> \
  --claims.paper-url https://example.org/paper.pdf \
  --claims.task-id claims_task_001 \
  --claims.audit-method llm \
  --claims.output-dir validator/v0/outputs/neuron \
  --claims.timeout 1800
```

Useful validator flags:

- `--claims.task-manifest /path/to/tasks.jsonl`: run a list of tasks.
- `--claims.audit-only`: score miners and write audit files without setting weights.
- `--claims.max-steps 1`: run one validation round and exit.
- `--claims.query-interval 60`: wait time between validation rounds.
- `--claims.require-validator-permit`: fail fast unless the hotkey has validator permit.

## Network Runbooks

The top-level commands above are network-agnostic. Detailed environment-specific
runbooks live in `docs/`:

- [Miner v0 and Validator v0](./docs/0009-v0-miner-validator.md)
- [Bittensor Localnet Operation](./docs/0010-bittensor-localnet.md)

## Data Contract

The core objects are:

- `Paper`
- `Span`
- `Claim`
- `EvidenceItem`
- `ClaimEvidenceLink`

See:

- [miner/v0/CLAIM_EXTRACTION_FIELDS.md](./miner/v0/CLAIM_EXTRACTION_FIELDS.md)
- [validator/v0/AUDIT_RECORD_FIELDS.md](./validator/v0/AUDIT_RECORD_FIELDS.md)
- [docs/0003-schema.md](./docs/0003-schema.md)

## Suggested Reading

1. [miner/v0/README.md](./miner/v0/README.md)
2. [validator/v0/README.md](./validator/v0/README.md)
3. [neurons/README.md](./neurons/README.md)
4. [docs/0009-v0-miner-validator.md](./docs/0009-v0-miner-validator.md)
5. [docs/0010-bittensor-localnet.md](./docs/0010-bittensor-localnet.md)

## Contributing

See [CONTRIBUTING.md](./CONTRIBUTING.md) for the contribution workflow.
