# Claims Neurons

The `neurons` package contains Bittensor-facing entry points for the Claims subnet.
The miner neuron now defaults to the skill-capable `agent_v1` miner. The
validator and wire envelope still retain Claims v0 compatibility while the
validator stack migrates to ARA-native scoring:

- `python -m neurons.miner` serves the `agent_v1` ARA miner through an axon by default.
- `python -m neurons.validator` queries registered miners, audits their responses with `validator.v0`, and sets weights.

The neuron scripts are intentionally thin. Core extraction behavior remains in
`miner.agent_v1`; core scoring behavior remains in `validator.v0` for the
current compatibility validator.

## Protocol

`ClaimExtractionSynapse` carries one extraction task:

- `protocol_version`: Claims protocol version, currently `claims.v0`
- `schema_version`: output schema version expected by the validator
- `task_id`: stable task identifier chosen by the validator
- `paper_id`: source paper identifier
- `paper_url`: downloadable PDF URL for network tasks
- `source_sha256`: optional expected SHA-256 hash for the PDF
- `artifact`: optional `ExtractionArtifact` JSON for local smoke tests
- `extraction`: miner response payload; `agent_v1` miners return the structured ARA projection, while legacy miners return the v0 `section_context_v1_output.json` shape
- `miner_version`: miner implementation version
- `error`: miner-side error message, if extraction fails

## Miner

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
  --claims.agent-runtime dspy-react \
  --claims.output-dir miner/agent_v1/outputs/neuron
```

The miner uses `miner.agent_v1` to process PDF URL tasks or artifact smoke-test
tasks supplied by the validator. It caches completed extractions by source hash,
protocol version, miner version, runtime, and model/parser configuration.

External agent loops can be used through the `agent-cli` runtime:

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
  --claims.agent-runtime agent-cli \
  --claims.agent-cli-command ".venv/bin/python -m miner.agent_v1.wrappers.hermes_prompt" \
  --claims.output-dir miner/agent_v1/outputs/neuron
```

Run the legacy direct miner explicitly with:

```bash
python -m neurons.miner \
  --netuid <NETUID> \
  --wallet.name <MINER_WALLET> \
  --wallet.hotkey <HOTKEY> \
  --subtensor.network <NETWORK> \
  --claims.pipeline v0 \
  --claims.extraction-mode abstract-full-paper \
  --claims.output-dir miner/v0/outputs/neuron
```

Use `--subtensor.chain_endpoint <WS_ENDPOINT>` instead of
`--subtensor.network <NETWORK>` for a custom chain endpoint.

## Validator

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

The validator loads URL tasks, sends them to registered miners, scores each
response with `validator.v0`, maintains a moving average, and sets weights on
the subnet. Use `--claims.task-artifact` for local smoke tests with a prebuilt
artifact, or `--claims.task-manifest` for a JSONL list of tasks.

Use `--claims.max-steps 1` for a single validation round.
Use `--claims.audit-only` to score miners without submitting weights.
Use `--subtensor.chain_endpoint <WS_ENDPOINT>` instead of
`--subtensor.network <NETWORK>` for a custom chain endpoint.
