# Claims Neurons

The `neurons` package contains Bittensor-facing entry points for the Claims subnet.
The protocol and neuron scripts wrap the file-based v0 miner and validator engines:

- `python -m neurons.miner` serves the v0 claim-evidence miner through an axon.
- `python -m neurons.validator` queries registered miners, audits their responses with `validator.v0`, and sets weights.

The neuron scripts are intentionally thin. Core extraction behavior remains in
`miner.v0`; core scoring behavior remains in `validator.v0`.

## Protocol

`ClaimExtractionSynapse` carries one extraction task:

- `protocol_version`: Claims protocol version, currently `claims.v0`
- `schema_version`: output schema version expected by the validator
- `task_id`: stable task identifier chosen by the validator
- `paper_id`: source paper identifier
- `paper_url`: downloadable PDF URL for network tasks
- `source_sha256`: optional expected SHA-256 hash for the PDF
- `artifact`: optional `ExtractionArtifact` JSON for local smoke tests
- `extraction`: miner response in the v0 `section_context_v1_output.json` shape
- `miner_version`: miner implementation version
- `error`: miner-side error message, if extraction fails

## Miner

```bash
python -m neurons.miner \
  --netuid 2 \
  --wallet.name test-miner \
  --wallet.hotkey default \
  --subtensor.chain_endpoint ws://127.0.0.1:9945 \
  --axon.port 8091 \
  --claims.pdf-extraction-method grobid
```

The miner uses `miner.v0` to process PDF URL tasks or artifact smoke-test tasks
supplied by the validator. It caches completed extractions by source hash,
protocol version, miner version, and model/parser configuration.

## Validator

```bash
python -m neurons.validator \
  --netuid 2 \
  --wallet.name test-validator \
  --wallet.hotkey default \
  --subtensor.chain_endpoint ws://127.0.0.1:9945 \
  --claims.paper-url https://example.org/paper.pdf \
  --claims.task-id claims_v0_localnet \
  --claims.audit-method deterministic
```

The validator loads URL tasks, sends them to registered miners, scores each
response with `validator.v0`, maintains a moving average, and sets weights on
the subnet. Use `--claims.task-artifact` for local smoke tests with a prebuilt
artifact, or `--claims.task-manifest` for a JSONL list of tasks.

Use `--claims.max-steps 1` for a single validation round.
Use `--claims.audit-only` to score miners without submitting weights.
