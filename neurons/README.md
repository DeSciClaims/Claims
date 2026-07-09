# Claims Neurons

The `neurons` package contains Bittensor-facing entry points for the Claims subnet.
The protocol and neuron scripts wrap the file-based v0 miner and validator engines:

- `python -m neurons.miner` serves the v0 claim-evidence miner through an axon.
- `python -m neurons.validator` queries registered miners, audits their responses with `validator.v0`, and sets weights.

The neuron scripts are intentionally thin. Core extraction behavior remains in
`miner.v0`; core scoring behavior remains in `validator.v0`.

## Protocol

`ClaimExtractionSynapse` carries one extraction task:

- `task_id`: stable task identifier chosen by the validator
- `paper_id`: source paper identifier
- `artifact`: input `ExtractionArtifact` JSON containing paper metadata and source spans
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
  --axon.port 8091
```

The miner uses `miner.v0` to process the artifact supplied by the validator.
It requires the same LLM and parsing environment used by `python -m miner.v0`.

## Validator

```bash
python -m neurons.validator \
  --netuid 2 \
  --wallet.name test-validator \
  --wallet.hotkey default \
  --subtensor.chain_endpoint ws://127.0.0.1:9945 \
  --claims.task-artifact miner/v0/outputs/section_context_v1__run_claims_v0/Rietveld_et_al_2013_Science/artifact.json \
  --claims.task-id claims_v0_localnet \
  --claims.audit-method deterministic
```

The validator loads one artifact, sends it to registered miners, scores each
response with `validator.v0`, maintains a moving average, and sets weights on
the subnet.

Use `--claims.max-steps 1` for a single validation round.
