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
```

PDF inputs use `pdf-inspector` by default. `GROBID_URL` is only required when
you explicitly choose `--pdf-extraction-method grobid`.

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
PDF inputs use `pdf-inspector` by default; set `--claims.pdf-extraction-method`
to `pypdf` or `grobid` when comparing readers.

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
  --claims.pdf-extraction-method pdf-inspector \
  --claims.batch-max-workers 2 \
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
  --claims.pdf-extraction-method pdf-inspector \
  --claims.batch-max-workers 2 \
  --claims.output-dir miner/agent_v1/outputs/neuron/testnet
```

Supported miner harnesses are `dspy-react`, `langchain-agent`, `hermes-cli`,
`codex-cli`, and `claude-cli`. For normal neuron runs, do not set
`CLAIMS_AGENT_INNER_COMMAND`; the harness/model flags derive it when needed.

Miner batch/PDF knobs:

- PDF reader: `--claims.pdf-extraction-method pdf-inspector|pypdf|grobid` or `SUBNET_CLAIMS_PDF_READER=...`. Default is `pdf-inspector`; `grobid` also needs `GROBID_URL`.
- Batch parallelism: `--claims.batch-max-workers N` or `CLAIMS_MINER_BATCH_MAX_WORKERS=N`. Default is `1`; use `2-3` when the model/provider can handle concurrent papers.
- Batch artifacts: set `--claims.backend-url` or `CLAIMS_BACKEND_URL` to the miner-upload API so full artifacts are uploaded outside dendrite responses.

For batch tasks, miners return one compact `articles[]` item per assigned
paper. `agent_v1` articles carry `agent_output`; the top-level `extraction` and
`source_payload` fields are reserved for single-paper compatibility.

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
  --claims.batch-score-rule mean \
  --claims.audit-method llm \
  --claims.validator-pipeline auto \
  --claims.rigor-harness hermes-cli \
  --claims.rigor-model openai/gpt-4o-mini \
  --claims.reference-harness codex-cli \
  --claims.reference-model gpt-5.5 \
  --claims.adjudication-harness dspy \
  --claims.adjudication-model-a openai/gpt-5 \
  --claims.adjudication-model-b anthropic/claude-sonnet-4 \
  --claims.adjudication-tiebreak-model google/gemini-2.5-pro \
  --claims.silver-adjudication-batch-size 8 \
  --claims.silver-adjudication-max-in-flight 32 \
  --claims.diagnostic-miner-max-workers 2 \
  --claims.diagnostic-max-workers 10 \
  --claims.diagnostic-miner-batch-size 10 \
  --claims.silver-paper-max-workers 10 \
  --claims.output-dir validator/agent_v1/outputs/neuron/testnet \
  --claims.timeout 1800
```

Useful validator flags:

- `--claims.backend-url http://127.0.0.1:8000`: use backend paper release and audit-record APIs.
- `--claims.batch-size 3`: request a random approved paper batch from the backend. The backend accepts larger V0 sampling batches when enough approved papers are available.
- `--claims.target-uid 1`: only query a specific miner UID. May be passed more than once for focused smoke tests.
- `--claims.topic economics`: filter backend-selected papers by topic. May be passed more than once.
- Silver batch score: mean across scoring-eligible papers. Miner misses count as zero; validator-failed papers are excluded for every miner.
- `--claims.rigor-harness hermes-cli --claims.rigor-model <MODEL>`: choose the diagnostic validation harness/model.
- `--claims.reference-harness codex-cli --claims.reference-model <MODEL>`: choose the private reference miner harness/model.
- `--claims.adjudication-harness dspy`: call adjudicator models in-process through DSPy/OpenRouter; CLI harnesses remain available.
- `--claims.adjudication-model-a/b/tiebreak-model <MODEL>`: choose the Silver adjudicator models.
- `--claims.silver-adjudication-batch-size 8`: adjudicate several anonymous cases per model call; use `1` to disable batching.
- `--claims.silver-adjudication-max-in-flight 32`: cap Silver model calls globally across papers and passes; use `0` for unlimited.
- Hermes adjudication defaults to a skill-based agent workflow with mode-`0600` task/schema files and validated JSON output. Set `CLAIMS_SILVER_ADJUDICATION_HERMES_EXECUTION_MODE=oneshot` for the optional tool-free path; use `CLAIMS_SILVER_ADJUDICATION_CLI_PROMPT_MODE=append` only for legacy CLI behavior.
- `CLAIMS_SILVER_WORKFLOW_MODE=file-agent`: use one file-workspace comparator, two anonymous judges plus a conditional tiebreaker, deterministic consensus, and one global Silver canonicalizer per paper. Agents receive short validator-owned `cN`, `kN`, and `uN` references; internal IDs are restored after validation. The legacy graph workflow remains the default.
- `--claims.diagnostic-miner-batch-size 10`: with a CLI rigor harness, review up to ten anonymized miners in one file-agent session per paper. `1` keeps the original per-miner path.
- `--claims.diagnostic-max-workers 10`: run paper/shard diagnostic jobs concurrently.
- `--claims.diagnostic-batch-max-input-bytes 8000000`: split unusually large diagnostic shards before the CLI context becomes unbounded; `0` disables this ceiling.
- `CLAIMS_DIAGNOSTIC_REPAIR_BATCH_SIZE=4`, `CLAIMS_DIAGNOSTIC_REPAIR_MAX_DEPTH=3`, and `CLAIMS_DIAGNOSTIC_REPAIR_MAX_WORKERS=2`: retry only missing or malformed reports in bounded shared-context shards before any individual fallback.
- `--claims.diagnostic-miner-max-workers 2`: control concurrent per-miner scoring and any individual diagnostic fallbacks.
- `--claims.skip-diagnostic-validation`: skip diagnostic reports when Silver is the only scoring path for a large run.
- `--claims.silver-paper-max-workers 10`: run Silver post-pass work for multiple batch papers concurrently.
- `--claims.silver-relation-mode dspy --claims.silver-relation-model <MODEL>`: classify comparison and consolidation edges with DSPy. `openai-compatible` and `cli` use the same batch contract; CLI requires `CLAIMS_SILVER_RELATION_CLI_COMMAND` and accepts prompts on stdin or through `{prompt_file}`.
- `--claims.allow-paper-reuse`: allow already assigned backend papers to be selected again for local smoke tests.
- `--claims.task-manifest /path/to/tasks.jsonl`: run a list of tasks.
- `--claims.audit-only`: calculate and persist proposed weights without submitting them on-chain.
- `CLAIMS_PAYOUT_MODE=winner-takes-most`: default payout. First place receives 70%; ranks 2-5 split 30% as 16/8/4/2. Ties share occupied slots.
- `CLAIMS_PAYOUT_WINNER_SHARE=0.70`, `CLAIMS_PAYOUT_RUNNER_UP_SLOTS=4`, `CLAIMS_PAYOUT_RUNNER_UP_DECAY=0.5`: payout controls.
- `--claims.max-steps 1`: run one validation round and exit.
- `--claims.query-interval 60` or `CLAIMS_QUERY_INTERVAL=60`: delay after one validation round finishes before the next starts.
- `--claims.run-heartbeat-interval 60`: keep backend run status live; interrupted runs close automatically.
- `--claims.require-validator-permit`: fail fast unless the hotkey has validator permit.

Optional Silver graph-pairing envs:

- `CLAIMS_SILVER_PAIRING_EMBEDDING_MODE=openrouter`: enable embedding retrieval before relation classification.
- `CLAIMS_SILVER_PAIRING_EMBEDDING_MODEL=nvidia/nemotron-3-embed-1b:free`: embedding model used for Bronze-to-miner and miner-to-Bronze top-k retrieval.
- `CLAIMS_SILVER_PAIRING_TOP_K=4`: candidate edges retained per retrieval direction.
- `CLAIMS_SILVER_CONSOLIDATION_TOP_K=4`: strongest post-adjudication consolidation neighbors retained per candidate; use `0` for the unbounded graph.
- `CLAIMS_SILVER_PAIRING_MAX_DENSE_PAIRS=64`: small candidate sets at or below this size also run dense pairing.
- `CLAIMS_SILVER_RELATION_BATCH_SIZE=16`: relation pairs per model request in comparison and consolidation; use `1` to disable batching.
- `CLAIMS_SILVER_RELATION_MAX_WORKERS=4`: relation-classification batches run concurrently up to this bounded worker count.
- `CLAIMS_SILVER_RELATION_BATCH_INPUT_TOKENS=100000`: split relation batches before their estimated input exceeds this budget.
- `CLAIMS_SILVER_ADJUDICATION_BATCH_INPUT_TOKENS=120000`: equivalent input budget for adjudication batches.
- `CLAIMS_SILVER_RELATION_BATCH_RETRIES=1` / `CLAIMS_SILVER_ADJUDICATION_BATCH_RETRIES=1`: retry a failed batch before recursively splitting it.
- `CLAIMS_SILVER_RELATION_WALL_TIMEOUT=900` / `CLAIMS_SILVER_ADJUDICATION_WALL_TIMEOUT=1800`: absolute stage deadlines in seconds; `0` disables the deadline.
- `CLAIMS_SILVER_RELATION_FALLBACK_MAX_CALLS=256` / `CLAIMS_SILVER_ADJUDICATION_FALLBACK_MAX_CALLS=256`: cap split fallback calls; `0` is unlimited.
- `CLAIMS_SILVER_PERSIST_CHUNK_SIZE=50`: cases, consensus, decisions, and score rows sent per persistence chunk.
- `CLAIMS_SILVER_PERSIST_VOTE_CHUNK_SIZE=150`: adjudication votes sent per persistence chunk.

File-workspace Silver controls:

- `CLAIMS_SILVER_FILE_AGENT_HARNESS=hermes-cli`: comparator and canonicalizer harness; `codex-cli` and `claude-cli` are also supported.
- `CLAIMS_SILVER_FILE_AGENT_COMPARISON_MODEL=<MODEL>` / `CLAIMS_SILVER_FILE_AGENT_CANONICALIZATION_MODEL=<MODEL>`: comparator and canonical draft models.
- `CLAIMS_SILVER_FILE_AGENT_CANONICAL_AUDIT_MODEL=<MODEL>`: independent model that audits and revises the canonical draft; defaults to adjudicator B.
- Existing adjudication harness and model settings control judge A, judge B, and the conditional tiebreaker.
- `CLAIMS_SILVER_FILE_AGENT_REQUIRE_DISTINCT_JUDGES=true`: require different models for direct judges A and B.
- `--claims.silver-adjudication-max-in-flight`: global process/model-call cap shared by all file-agent stages.
- `CLAIMS_SILVER_FILE_AGENT_TIMEOUT=1800`: absolute timeout for each file-agent execution.
- `CLAIMS_SILVER_FILE_AGENT_MAX_TURNS=30`: Hermes turn budget for comparator, judge, canonicalizer, and canonical-audit agents.
- `CLAIMS_SILVER_FILE_AGENT_MAX_TOKENS=32768`: per-turn Hermes output budget for all file-agent stages.
- `CLAIMS_SILVER_FILE_AGENT_USAGE_GRACE_SECONDS=15`: allow a completed CLI to emit its usage footer before forced cleanup.
- `CLAIMS_SILVER_FILE_AGENT_FALLBACK=none`: fail the paper instead of falling back to the legacy stage.
- `CLAIMS_MODEL_PRICING_JSON='{"model":{"input":1,"output":2}}'`: optional USD-per-million-token rates when a subscription CLI reports tokens but no dollar cost.

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
