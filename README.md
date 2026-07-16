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

Native runtimes:

```bash
python -m miner.agent_v1 --pdf /path/to/paper.pdf --runtime dspy-react --output-dir miner/agent_v1/outputs/my_run
python -m miner.agent_v1 --pdf /path/to/paper.pdf --runtime langchain-agent --output-dir miner/agent_v1/outputs/my_run
```

External CLI agent runtimes use `agent-cli` plus a wrapper:

```bash
SUBNET_CLAIMS_AGENT_CLI_COMMAND=".venv/bin/python -m miner.agent_v1.wrappers.codex_prompt" \
python -m miner.agent_v1 \
  --pdf /path/to/paper.pdf \
  --runtime agent-cli \
  --output-dir miner/agent_v1/outputs/my_run
```

For Hermes:

```bash
CLAIMS_AGENT_INNER_COMMAND="hermes chat --provider openrouter -m openai/gpt-4o-mini --max-turns 30 -q" \
SUBNET_CLAIMS_AGENT_CLI_COMMAND=".venv/bin/python -m miner.agent_v1.wrappers.hermes_prompt" \
python -m miner.agent_v1 \
  --pdf /path/to/paper.pdf \
  --runtime agent-cli \
  --output-dir miner/agent_v1/outputs/my_run
```

See [miner/agent_v1/README.md](./miner/agent_v1/README.md) and
[miner/agent_v1/wrappers/README.md](./miner/agent_v1/wrappers/README.md) for
the SkillPack contract and runtime options.

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

For Codex as the rigor backend:

```bash
SUBNET_CLAIMS_VALIDATOR_AGENT_CLI_COMMAND=".venv/bin/python -m validator.agent_v1.wrappers.codex_prompt" \
python -m validator.agent_v1 \
  --agent-json outputs/my_run/agent_output.json \
  --source-payload outputs/my_run/source_payload.json \
  --runtime agent-cli \
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
  --claims.agent-runtime dspy-react \
  --claims.output-dir miner/agent_v1/outputs/neuron/testnet
```

`agent_v1` is the default `--claims.pipeline`. To run an external CLI backend:

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

Use `--claims.agent-runtime dspy-react` or `--claims.agent-runtime langchain-agent`
for native Python agent runtimes.

Legacy v0 neuron commands are documented separately in
[docs/0009-v0-miner-validator.md](./docs/0009-v0-miner-validator.md) and should
not be used for new testnet miners.

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
  --claims.validator-pipeline auto \
  --claims.output-dir validator/agent_v1/outputs/neuron/testnet \
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

- [Agent v1 Canonical Miner](./docs/0011-agent-v1-canonical-miner.md)
- [Agent v1 Validator, ARA Seal, and Benchmarks](./docs/0013-agent-v1-validator-seal-and-benchmarks.md)
- [Miner v0 and Validator v0](./docs/0009-v0-miner-validator.md)
- [Bittensor Localnet Operation](./docs/0010-bittensor-localnet.md)

## Data Contract

The miner output is a Claims agent artifact derived from the ARA markdown model:

- `paper`
- `logic.claims`
- `logic.concepts`
- `logic.experiments`
- `evidence.records`
- `trace`
- `src`
- `metadata.runtime_metrics`

The legacy Claims v0 objects are still used by compatibility paths and the
current validator audit stack:

- `Paper`
- `Span`
- `Claim`
- `EvidenceItem`
- `ClaimEvidenceLink`

See:

- [docs/0012-ara-vs-claims-v0-schema.md](./docs/0012-ara-vs-claims-v0-schema.md)
- [miner/v0/CLAIM_EXTRACTION_FIELDS.md](./miner/v0/CLAIM_EXTRACTION_FIELDS.md)
- [validator/v0/AUDIT_RECORD_FIELDS.md](./validator/v0/AUDIT_RECORD_FIELDS.md)
- [docs/0003-schema.md](./docs/0003-schema.md)

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
