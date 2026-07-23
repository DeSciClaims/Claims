# Claims Neurons

The `neurons` package contains Bittensor-facing entry points for the Claims subnet.
The miner neuron now defaults to the skill-capable `agent_v1` miner. The
validator and wire envelope still retain Claims v0 compatibility while the
validator stack migrates to ARA-native scoring:

- `python -m neurons.miner` serves the `agent_v1` ARA miner through an axon by default.
- `python -m neurons.validator` queries registered miners, routes responses to
  `validator.agent_v1` or `validator.v0`, and sets weights.

The neuron scripts are intentionally thin. Core extraction behavior remains in
`miner.agent_v1`; canonical ARA scoring lives in `validator.agent_v1`, with
`validator.v0` retained for legacy response compatibility.

New testnet miners should run `agent_v1`. Legacy v0 commands live in
[`docs/0009-v0-miner-validator.md`](../docs/0009-v0-miner-validator.md) for
compatibility reproduction only.

## Protocol

`ClaimExtractionSynapse` carries either a legacy single-paper task or a backend
batch task:

- `protocol_version`: Claims protocol version, currently `claims.v0`
- `schema_version`: output schema version expected by the validator
- `task_id`: stable task identifier chosen by the validator
- `batch_id`: backend batch identifier when the task came from the Claims API
- `selection_seed`: backend sampling seed recorded for auditability
- `task_version`: task contract version, currently `claims_task_v0`
- `scoring_version`: scoring contract version, currently `agent_v1_pass4_deterministic_v0`
- `papers`: backend-selected paper list for batch tasks
- `paper_id`: source paper identifier
- `paper_url`: downloadable PDF URL for network tasks
- `source_sha256`: optional expected SHA-256 hash for the PDF
- `artifact`: optional `ExtractionArtifact` JSON for local smoke tests
- `articles`: miner response list for batch tasks, one item per assigned paper
- `extraction`: miner response payload; `agent_v1` miners return the structured ARA projection, while legacy miners return the v0 `section_context_v1_output.json` shape
- `source_payload`: source spans returned by `agent_v1` miners for grounding checks
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
  --claims.pipeline agent_v1 \
  --claims.agent-runtime dspy-react \
  --claims.output-dir miner/agent_v1/outputs/neuron/testnet
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
  --claims.pipeline agent_v1 \
  --claims.agent-runtime agent-cli \
  --claims.agent-cli-command ".venv/bin/python -m miner.agent_v1.wrappers.hermes_prompt" \
  --claims.output-dir miner/agent_v1/outputs/neuron/testnet
```

Use `--subtensor.chain_endpoint <WS_ENDPOINT>` instead of
`--subtensor.network <NETWORK>` for a custom chain endpoint.

## Validator

```bash
CLAIMS_BACKEND_URL=http://127.0.0.1:8000 \
python -m neurons.validator \
  --netuid <NETUID> \
  --wallet.name <VALIDATOR_WALLET> \
  --wallet.hotkey <HOTKEY> \
  --subtensor.network <NETWORK> \
  --claims.network testnet \
  --claims.backend-url http://127.0.0.1:8000 \
  --claims.batch-size 3 \
  --claims.batch-score-rule min \
  --claims.audit-method llm \
  --claims.validator-pipeline auto \
  --claims.output-dir validator/agent_v1/outputs/neuron/testnet \
  --claims.timeout 1800
```

The validator asks the backend for a random approved paper batch, sends the
batch to registered miners, scores each paper response, aggregates the batch
score with `--claims.batch-score-rule`, maintains a moving average, posts audit
records back to the backend, and sets weights on the subnet.
The backend records the selected batch immediately and excludes assigned papers
from future selections unless the validator passes `--claims.allow-paper-reuse`
for a smoke test.
By default `--claims.validator-pipeline auto` routes ARA-shaped responses to
`validator.agent_v1` and legacy responses to `validator.v0`.
Use `--claims.task-artifact` for local smoke tests with a prebuilt artifact, or
`--claims.task-manifest` for a JSONL list of tasks.

Force ARA-native scoring with:

```bash
python -m neurons.validator \
  --netuid <NETUID> \
  --wallet.name <VALIDATOR_WALLET> \
  --wallet.hotkey <HOTKEY> \
  --subtensor.network <NETWORK> \
  --claims.paper-url https://example.org/paper.pdf \
  --claims.task-id claims_task_001 \
  --claims.validator-pipeline agent_v1 \
  --claims.agent-v1-runtime agent-cli \
  --claims.output-dir validator/agent_v1/outputs/neuron \
  --claims.timeout 1800
```

For smoke tests without the rigor agent, add `--claims.agent-v1-skip-rigor`.

Use `--claims.max-steps 1` for a single validation round.
Use `--claims.audit-only` to score miners without submitting weights.
Use `--subtensor.chain_endpoint <WS_ENDPOINT>` instead of
`--subtensor.network <NETWORK>` for a custom chain endpoint.
