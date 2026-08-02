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

The canonical miner pipeline is `agent_v1`: a skill-capable agent miner that
uses the [ARA](https://github.com/ARA-Labs/Agent-Native-Research-Artifact)
compiler skill and writes Claims-owned structured agent artifacts derived from
the ARA markdown artifact model.
The older `v0` direct model pipeline remains available only as a legacy
compatibility path while the validator and network envelope continue to support
existing Claims v0 tasks.

## Repository Layout

```text
Claims/
├── miner/agent_v1/    # canonical skill-capable agent miner pipeline
├── miner/v0/          # legacy direct claim extraction pipeline
├── validator/agent_v1/# canonical agent artifact validation pipeline
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

Use `agent_v1` for new miner runs. It mounts the ARA compiler skill, runs an
agent loop, validates the structured agent JSON artifact, and records runtime
metadata such as elapsed time, attempts, token usage, and cost when the backend
exposes it.

Compile from a PDF:

```bash
python -m miner.agent_v1 \
  --pdf /path/to/paper.pdf \
  --runtime dspy-react \
  --output-dir miner/agent_v1/outputs/my_run
```

Compile from a text extraction:

```bash
python -m miner.agent_v1 \
  --text /path/to/paper.txt \
  --runtime dspy-react \
  --output-dir miner/agent_v1/outputs/my_run
```

The canonical structured output is:

```text
miner/agent_v1/outputs/<run>/agent_output.json
```

Each run also writes:

```text
miner/agent_v1/outputs/<run>/PAPER.md
miner/agent_v1/outputs/<run>/backend_manifest.json
miner/agent_v1/outputs/<run>/skill_manifest.json
miner/agent_v1/outputs/<run>/agent_validation_report.json
```

### Agent Runtime Options

The direct `miner.agent_v1` CLI still uses low-level runtime names:

```bash
python -m miner.agent_v1 --pdf /path/to/paper.pdf --runtime dspy-react --output-dir miner/agent_v1/outputs/my_run
python -m miner.agent_v1 --pdf /path/to/paper.pdf --runtime langchain-agent --output-dir miner/agent_v1/outputs/my_run
```

For live Bittensor neurons, prefer the higher-level harness/model flags shown
below. They derive the runtime, wrapper, and inner CLI command automatically.

See [miner/agent_v1/README.md](./miner/agent_v1/README.md) and
[miner/agent_v1/wrappers/README.md](./miner/agent_v1/wrappers/README.md) for
the SkillPack contract and lower-level wrapper options.

## Run The Validator Locally

Use `validator.agent_v1` for Claims agent miner outputs. It runs deterministic
structural and grounding checks, then a required agent rigor pass.

```bash
python -m validator.agent_v1 \
  --agent-json outputs/my_run/agent_output.json \
  --source-payload outputs/my_run/source_payload.json \
  --runtime dspy-react \
  --output-dir outputs/my_run_validation
```

See [validator/agent_v1/README.md](./validator/agent_v1/README.md) for backend
configuration and output files.

Legacy v0 miner and validator commands are intentionally kept out of the main
quickstart. Use [docs/0009-v0-miner-validator.md](./docs/0009-v0-miner-validator.md)
only when reproducing older compatibility runs.

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
  --claims.pipeline agent_v1 \
  --claims.agent-harness dspy-react \
  --claims.agent-model openrouter/openai/gpt-5-mini \
  --claims.output-dir miner/agent_v1/outputs/neuron/testnet
```

`agent_v1` is the default `--claims.pipeline`. To run Hermes Agent CLI:

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
  --claims.agent-harness hermes-cli \
  --claims.agent-model openai/gpt-5-mini \
  --claims.output-dir miner/agent_v1/outputs/neuron/testnet
```

Supported miner harnesses are `dspy-react`, `langchain-agent`, `hermes-cli`,
`codex-cli`, and `claude-cli`. For normal neuron runs, do not set
`CLAIMS_AGENT_INNER_COMMAND`; the harness/model flags derive it when needed.

Legacy v0 neuron commands are documented separately in
[docs/0009-v0-miner-validator.md](./docs/0009-v0-miner-validator.md) and should
not be used for new testnet miners.

Use `--subtensor.chain_endpoint <WS_ENDPOINT>` instead of
`--subtensor.network <NETWORK>` when connecting to a custom chain endpoint.

## Run A Bittensor Validator

Start a validator neuron after the validator hotkey is registered and ready to
submit weights. The validator gets paper batches from the Claims backend,
queries miners over Bittensor, runs diagnostic validation, optionally creates
Bronze through the reference miner, runs Silver adjudication, posts records
back to the backend, and then sets weights.

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
  --claims.target-uid <MINER_UID> \
  --claims.batch-score-rule min \
  --claims.audit-method llm \
  --claims.validator-pipeline auto \
  --claims.rigor-harness hermes-cli \
  --claims.rigor-model openai/gpt-4o-mini \
  --claims.reference-harness codex-cli \
  --claims.reference-model gpt-5.5 \
  --claims.adjudication-harness hermes-cli \
  --claims.adjudication-model-a openai/gpt-5 \
  --claims.adjudication-model-b anthropic/claude-sonnet-4 \
  --claims.adjudication-tiebreak-model google/gemini-2.5-pro \
  --claims.output-dir validator/agent_v1/outputs/neuron/testnet \
  --claims.timeout 1800
```

Useful validator flags:

- `--claims.backend-url http://127.0.0.1:8000`: use backend paper release and audit-record APIs.
- `--claims.batch-size 3`: request a random approved paper batch from the backend.
- `--claims.target-uid 1`: only query a specific miner UID. May be passed more than once for focused smoke tests.
- `--claims.topic economics`: filter backend-selected papers by topic. May be passed more than once.
- `--claims.batch-score-rule min`: score the batch by the lowest per-paper score, the current highest-minimum rule.
- `--claims.rigor-harness hermes-cli --claims.rigor-model <MODEL>`: choose the diagnostic validation harness/model.
- `--claims.reference-harness codex-cli --claims.reference-model <MODEL>`: choose the private reference miner harness/model.
- `--claims.adjudication-harness hermes-cli`: choose the Silver adjudication harness.
- `--claims.adjudication-model-a/b/tiebreak-model <MODEL>`: choose the Silver adjudicator models.
- `--claims.allow-paper-reuse`: allow already assigned backend papers to be selected again for local smoke tests.
- `--claims.task-manifest /path/to/tasks.jsonl`: run a list of tasks.
- `--claims.audit-only`: score miners and write audit files without setting weights.
- `--claims.max-steps 1`: run one validation round and exit.
- `--claims.query-interval 60`: wait time between validation rounds.
- `--claims.require-validator-permit`: fail fast unless the hotkey has validator permit.

For local smoke tests without the backend, pass exactly one of
`--claims.paper-url`, `--claims.task-artifact`, or `--claims.task-manifest`.

## Suggested Reading

1. [miner/agent_v1/README.md](./miner/agent_v1/README.md)
2. [docs/0011-agent-v1-canonical-miner.md](./docs/0011-agent-v1-canonical-miner.md)
3. [docs/0012-ara-vs-claims-v0-schema.md](./docs/0012-ara-vs-claims-v0-schema.md)
4. [docs/0013-agent-v1-validator-seal-and-benchmarks.md](./docs/0013-agent-v1-validator-seal-and-benchmarks.md)
5. [miner/agent_v1/wrappers/README.md](./miner/agent_v1/wrappers/README.md)
6. [validator/v0/README.md](./validator/v0/README.md)
7. [neurons/README.md](./neurons/README.md)

## Contributing

See [CONTRIBUTING.md](./CONTRIBUTING.md) for the contribution workflow.
