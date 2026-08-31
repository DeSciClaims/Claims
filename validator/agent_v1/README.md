# validator.agent_v1

`validator.agent_v1` validates canonical `miner.agent_v1` agent JSON outputs. It
uses deterministic checks for schema, cross references, and source-ref contract
validity, then runs a required agent rigor pass over semantic grounding and ARA
Seal-style rigor dimensions.

The first supported rigor backends are:

- `dspy-react`: native DSPy model call.
- `langchain-agent`: native LangChain agent call.
- `agent-cli` with `validator.agent_v1.wrappers.codex_prompt`: external Codex
  agent loop with the same file contract.

## Validator Profiles

[validator.testnet.env.example](./validator.testnet.env.example) is a complete
50-paper, fifteen-miner SN111 testnet profile for the full Bittensor validator. Miners
are selected adaptively unless `CLAIMS_TARGET_UIDS` is explicitly enabled. It configures
paper-level diagnostics, Bronze generation, anonymous Silver judges, the
file-agent comparator/canonicalizer/auditor, persistence limits, and one scoring
cycle. It deliberately uses different models for independent pipeline roles.

[validator.mainnet.env.example](./validator.mainnet.env.example) carries the
current SN111 mainnet operating policy: 50 papers, fifteen adaptive miners, four
cycles with a three-hour post-completion delay, one-hour miner timeout, file-agent Silver, and
winner-takes-most payouts. Its model fields are intentionally blank. Mainnet
validators must choose their own role-specific models and provider credentials.

For a manual or Ubuntu installation:

```bash
cp validator/agent_v1/validator.testnet.env.example .env
# Set OPENROUTER_API_KEY and review CLAIMS_TARGET_UIDS.

python -m neurons.validator \
  --netuid 530 \
  --wallet.name claims-test-validator \
  --wallet.hotkey default \
  --subtensor.network test \
  --logging.debug
```

For mainnet, copy the mainnet profile and fill every blank credential and model
field before starting the command shown in the top-level README:

```bash
cp validator/agent_v1/validator.mainnet.env.example .env
```

The Docker entrypoint reads the same settings through `--env-file` or from
`/data/env/validator.env`. Set `CLAIMS_BRONZE_ROOT=/data/bronze` and
`CLAIMS_OUTPUT_DIR=/data/outputs/validator` so generated state remains on the
persistent volume. Set `CLAIMS_OUTPUT_RETENTION_RUNS=5` to keep the newest five
local `run_*` outputs after each successful run. The current run, reusable
Bronze records, model-usage recovery files, wallets, and backend records are
not removed. Set it to `0` to disable cleanup. Wallet files are never included
in the profile or image.

## Validator Neuron Configuration

The top-level README intentionally shows only the normal launch arguments.
Provider credentials, role-specific harnesses and models, concurrency, repair,
and persistence controls belong in `.env`. Command-line arguments override the
corresponding environment values.

### Run Identity And Scheduling

- `--netuid`, `--wallet.name`, and `--wallet.hotkey` identify the registered
  validator hotkey. `--subtensor.network` selects `test`, `finney`, or another
  configured Bittensor network; `--subtensor.chain_endpoint` may instead point
  to a specific WebSocket endpoint.
- `--claims.network testnet|mainnet` selects the Claims backend data partition.
  It is independent of `--subtensor.network`.
- `--claims.backend-url` enables signed batch selection, run records, canonical
  miner assignments, artifact reuse, Silver persistence, and dashboard data.
- `--claims.batch-size` is the number of papers requested from the backend.
  `--claims.topic` and `--claims.paper-id` may be repeated to constrain paper
  selection. `--claims.allow-paper-reuse` lets the backend select approved
  papers that were assigned to earlier batches. The supplied validator profiles
  currently enable it while the production paper catalog is still growing.
- `--claims.output-dir` stores validator-side artifacts. `--claims.timeout` is
  the dendrite deadline for the complete miner batch response.
- `--claims.max-steps N` exits after `N` scoring cycles; `0` runs indefinitely.
  `--claims.query-interval` controls the delay after each completed cycle.
- `--claims.run-heartbeat-interval` updates backend liveness while a cycle is
  running. `--claims.require-validator-permit` fails startup when the selected
  hotkey lacks a validator permit.
- `--claims.audit-only` calculates and persists proposed scores and weights but
  does not submit weights on chain.

For local operation without the backend, pass exactly one of
`--claims.paper-url`, `--claims.task-artifact`, or `--claims.task-manifest`.

### Canonical Batches And Miner Selection

The backend keeps each immutable paper and miner assignment current for the
configured rolling reuse lifetime. The first validator creates the batch and
atomically proposes the miner set; later validators receive the same task ID,
batch ID, papers, seed, and UID-to-hotkey snapshot. At or after expiry, the first
new request creates the successor. One validator owns a temporary collection
lease and queries unfinished miners. Followers poll and reuse completed
batch-scoped artifacts instead of repeating miner inference.

- `--claims.target-uid UID` is an exact operator override and may be repeated.
  If an existing canonical assignment contains different UIDs, the validator
  refuses to diverge. Use a backend assignment lifetime of `0` for isolated tests.
- `--claims.miner-selection-mode adaptive --claims.miner-sample-size 15`
  enables UID-only V0 selection: six qualification, six performance, and three
  rotation slots. Performance sampling is seeded and weighted by
  `0.10 + mean(last three scores)`.
- A miner remains fully vetted only after three evaluations. Until six vetted
  miners are available, under-vetted miners with at least one positive stored
  score may fill otherwise-empty performance slots. They are recorded as
  `performance-provisional`; zero-only histories do not qualify. This fallback
  stops being used automatically whenever six vetted candidates are available.
  Any performance seats still vacant after that use the normal oldest-evaluation
  fallback and are recorded as `performance-fallback`.
- Qualification covers UIDs with fewer than three evaluations. The normal order
  is fewest evaluations, immunity urgency, oldest selection, then UID. When
  fallback candidates have equally old evaluations, miners registered at or
  after the backend-provided subnet update block are preferred by lowest UID;
  older registrations fill only any remaining shortage.
- `CLAIMS_MINER_IMMUNITY_PERIOD_BLOCKS=0` reads the subnet immunity period from
  chain. `CLAIMS_MINER_IMMUNITY_PRIORITY_BLOCKS=7200` prioritizes under-vetted
  UIDs during approximately their final 24 immunity hours.
- Adaptive candidates must be registered, expose a serving Axon, and not be the
  validator's own hotkey. Assigned miners are never substituted after selection.
  Missing, invalid, offline, and timed-out miners remain in the batch and score
  zero.
- `CLAIMS_BATCH_COLLECTION_POLL_SECONDS=5` controls follower polling.
  `CLAIMS_BATCH_COLLECTION_WAIT_SECONDS` bounds the wait and should exceed
  `CLAIMS_TIMEOUT`; the default is at least the miner timeout plus 300 seconds.

Selection state is stored by validator, network, netuid, and UID, then rebuilt
from subnet-wide evaluation events. It records evaluation count, distinct batch
count, the last three scores, last selection, and last evaluation. A changed
registration block resets a UID's state.

### Scoring Pipeline

- `--claims.audit-method llm` and `--claims.agent-v1-validation-mode llm` enable
  semantic diagnostic validation. `--claims.validator-pipeline auto` routes
  ARA-shaped submissions to `agent_v1` while preserving legacy compatibility.
- `--claims.silver-enable` runs Bronze lookup, comparison, adjudication,
  canonicalization, and miner-vs-Silver scoring after diagnostics.
- Silver batch scores are means over scoring-eligible papers. Miner misses count
  as zero; validator-failed papers are excluded for every miner.
- `--claims.payout-mode winner-takes-most` allocates 70% to rank 1 and
  16/8/4/2% to ranks 2-5. Ties share the occupied rank slots. The related envs
  are `CLAIMS_PAYOUT_WINNER_SHARE`, `CLAIMS_PAYOUT_RUNNER_UP_SLOTS`, and
  `CLAIMS_PAYOUT_RUNNER_UP_DECAY`.
- `--claims.batch-score-rule` controls legacy diagnostic aggregation. Final
  Silver incentives use the mean regardless of that compatibility setting.

### Harnesses And Models

Set each role independently; production validators should not assume that the
testnet example models are a recommended mainnet ensemble.

- `--claims.rigor-harness` / `--claims.rigor-model`: semantic diagnostic role.
- `--claims.reference-harness` / `--claims.reference-model`: Bronze reference
  generation role. `CLAIMS_REFERENCE_MINER_COMMAND=python -m
  claims_reference_miner` enables local reference generation.
- `--claims.adjudication-harness`: Silver judge runtime. Supported modes include
  DSPy/OpenAI-compatible calls and `hermes-cli`, `codex-cli`, or `claude-cli`.
- `--claims.adjudication-model-a`, `--claims.adjudication-model-b`, and
  `--claims.adjudication-tiebreak-model`: two direct judges and the conditional
  tiebreaker.
- `CLAIMS_SILVER_FILE_AGENT_HARNESS` and the comparison, canonicalization, and
  canonical-audit model envs configure the remaining file-workspace roles.
- `CLAIMS_MODEL_PRICING_JSON='{"model":{"input":1,"output":2}}'` supplies
  optional USD-per-million-token rates when a CLI reports tokens without cost.

### Diagnostics And Capacity

- `--claims.diagnostic-miner-batch-size 10` enables one diagnostic file-agent
  operation per paper that reviews all available miners; it is not a shard size.
  Sparse claim assessments report only issues, and an empty map is valid. Set
  the value to `1` for the original per-miner path.
- `--claims.diagnostic-max-workers` controls concurrent paper diagnostics.
  `--claims.diagnostic-miner-max-workers` controls per-miner fallback concurrency.
- `--claims.skip-diagnostic-validation` omits diagnostic reports when only the
  Silver path is required.
- `--claims.silver-paper-max-workers` controls concurrent paper-level Silver
  pipelines. `--claims.silver-adjudication-max-in-flight` is the global cap on
  simultaneous Silver model calls; `0` removes that cap.
- `--claims.silver-adjudication-batch-size` sets anonymous cases per judge call;
  `1` disables batching. `CLAIMS_SILVER_ADJUDICATION_BATCH_INPUT_TOKENS` bounds
  estimated input size before a batch is split.
- `--claims.silver-max-eligible-claims-per-miner` bounds miner candidates entering
  Silver. `--claims.silver-max-adjudication-cases-per-paper` bounds primary judge
  cases and shares capacity fairly across miners.
- `--claims.silver-filter-by-assessment true|false` controls whether diagnostic
  issue assessments exclude miner claims downstream. The default is `false`;
  findings can still affect diagnostic quality without filtering candidates.

### Pairing, Repair, And Persistence

- `CLAIMS_SILVER_PAIRING_EMBEDDING_MODE` and
  `CLAIMS_SILVER_PAIRING_EMBEDDING_MODEL` enable embedding retrieval before
  relation classification.
- `CLAIMS_SILVER_PAIRING_TOP_K` controls candidate neighbors per retrieval
  direction. `CLAIMS_SILVER_CONSOLIDATION_TOP_K` controls post-adjudication
  consolidation neighbors; `0` is unbounded.
- `CLAIMS_SILVER_PAIRING_MAX_DENSE_PAIRS` enables dense pairing for candidate
  sets at or below the configured size; `0` disables the dense addition.
- `CLAIMS_SILVER_RELATION_BATCH_SIZE`, `CLAIMS_SILVER_RELATION_MAX_WORKERS`, and
  `CLAIMS_SILVER_RELATION_BATCH_INPUT_TOKENS` control relation request packing
  and concurrency.
- `CLAIMS_SILVER_RELATION_BATCH_RETRIES` and
  `CLAIMS_SILVER_ADJUDICATION_BATCH_RETRIES` retry a failed batch before
  recursive splitting. The corresponding `*_WALL_TIMEOUT` and
  `*_FALLBACK_MAX_CALLS` envs bound total time and split calls; `0` disables a
  bound.
- `CLAIMS_SILVER_PERSIST_CHUNK_SIZE` controls case, consensus, decision, and
  score writes. `CLAIMS_SILVER_PERSIST_VOTE_CHUNK_SIZE` controls vote writes.

### File-Workspace Silver

`CLAIMS_SILVER_WORKFLOW_MODE=file-agent` runs one comparator, two anonymous
judges plus a conditional tiebreaker, deterministic consensus, a canonical
draft, and an independent canonical audit per paper. Agents use validator-owned
short references; internal IDs are restored after validation.

- `CLAIMS_SILVER_FILE_AGENT_REQUIRE_DISTINCT_JUDGES=true` requires different
  models for direct judges A and B.
- `CLAIMS_SILVER_FILE_AGENT_TIMEOUT` is the deadline for each agent execution.
  `CLAIMS_SILVER_FILE_AGENT_MAX_TURNS` and `MAX_TOKENS` bound its turn and output
  budgets.
- `CLAIMS_SILVER_FILE_AGENT_USAGE_GRACE_SECONDS` allows a completed CLI to emit
  its usage footer before cleanup.
- `CLAIMS_SILVER_FILE_AGENT_FALLBACK=none` fails that paper rather than silently
  reverting to the legacy graph workflow.
- Hermes adjudication uses skill-based artifact execution by default.
  `CLAIMS_SILVER_ADJUDICATION_HERMES_EXECUTION_MODE=oneshot` enables the optional
  tool-free mode; `CLAIMS_SILVER_ADJUDICATION_CLI_PROMPT_MODE=append` exists only
  for legacy CLI behavior.

## Outputs

Each run writes:

- `agent_v1_validation_report.json`: final score, pass summaries, findings,
  token/cost metadata when available.
- `structural_findings.json`: deterministic schema and reference findings.
- `grounding_findings.json`: deterministic source-ref contract findings.
- `rigor_findings.json`: agent semantic rigor findings.
- `rigor_backend_manifest.json`: runtime metadata for the rigor agent backend.

## DSPy

```bash
OPENROUTER_API_KEY=... \
SUBNET_CLAIMS_VALIDATOR_AGENT_MODEL=openrouter/openai/gpt-4o-mini \
.venv/bin/python -m validator.agent_v1 \
  --agent-json outputs/rietveld_agent_v1_dspy/agent_output.json \
  --source-payload outputs/rietveld_agent_v1_dspy/source_payload.json \
  --runtime dspy-react \
  --max-agent-iters 4 \
  --output-dir outputs/validate_rietveld_dspy
```

## LangChain

```bash
OPENROUTER_API_KEY=... \
SUBNET_CLAIMS_VALIDATOR_AGENT_MODEL=openrouter/openai/gpt-4o-mini \
.venv/bin/python -m validator.agent_v1 \
  --agent-json outputs/rietveld_agent_v1_dspy/agent_output.json \
  --source-payload outputs/rietveld_agent_v1_dspy/source_payload.json \
  --runtime langchain-agent \
  --max-agent-iters 4 \
  --output-dir outputs/validate_rietveld_langchain
```

## Codex CLI

```bash
SUBNET_CLAIMS_VALIDATOR_AGENT_CLI_COMMAND=".venv/bin/python -m validator.agent_v1.wrappers.codex_prompt" \
.venv/bin/python -m validator.agent_v1 \
  --agent-json outputs/rietveld_agent_v1_codex/agent_output.json \
  --source-payload outputs/rietveld_agent_v1_codex/source_payload.json \
  --runtime agent-cli \
  --output-dir outputs/validate_rietveld_codex
```

The Codex wrapper defaults to:

```bash
codex exec --json --sandbox workspace-write --skip-git-repo-check
```

Set `CLAIMS_VALIDATOR_AGENT_INNER_COMMAND` to override the inner command.

## Deterministic Smoke

Use `--skip-rigor-agent` to test file flow without model calls. This is a smoke
mode only; production scoring should include the rigor agent.

```bash
.venv/bin/python -m validator.agent_v1 \
  --agent-json outputs/rietveld_agent_v1_dspy/agent_output.json \
  --source-payload outputs/rietveld_agent_v1_dspy/source_payload.json \
  --output-dir outputs/validate_smoke \
  --skip-rigor-agent
```

## Paper-Level CLI Diagnostics

The validator can share one CLI session across every available anonymized miner
for a paper while preserving one diagnostic report and score per miner:

```bash
CLAIMS_AUDIT_METHOD=llm \
CLAIMS_RIGOR_HARNESS=hermes-cli \
CLAIMS_RIGOR_MODEL=openai/gpt-4o-mini \
CLAIMS_DIAGNOSTIC_MINER_BATCH_SIZE=10 \
python -m neurons.validator ...
```

Identical source payloads are stored once. The same operation emits sparse rigor
findings and one compact evidence/relevance assessment per claim. Miner shards
and recursive repair are not used. The validator preallocates every anonymized
submission/claim slot and permits one targeted repair call for unresolved slots.
Valid reports survive; only submissions still incomplete after repair fail.

## Silver Scoring Smoke

The Silver path includes Bronze lookup, adjudication cases, Silver record
construction, miner-vs-Silver scoring, backend persistence, and public feedback.
Batch scores are means across scoring-eligible papers. Miner misses remain zero;
validator-side paper failures are excluded for every miner and mark the completed
run degraded. Weights default to the 70/30 winner-takes-most rank curve.

```bash
/Users/ogbanugot/miniconda3/bin/conda run -n claims_subnet \
  python Claims/tests/smoke_silver_e2e.py
```

That smoke starts a local backend, signs validator requests with a temporary
hotkey, writes Bronze/Silver/score records, then reads the public miner Silver
feedback endpoint.

## File-Workspace Silver

Set `CLAIMS_SILVER_WORKFLOW_MODE=file-agent` to run the current per-paper
workspace pipeline: global comparison, anonymous parallel judges, conditional
tiebreak, deterministic consensus, canonical draft, and independent canonical
audit/revision. Exact restatements and adjudicated same-unit groups cannot be
split, and candidates without linked evidence cannot receive Silver credit.
Agents use short `cN`, `kN`, and `uN` references; the validator owns the mapping
to persisted candidate, case, and Silver lineage IDs.
Only the logical workspace ID and manifest hash are persisted.

```bash
CLAIMS_SILVER_WORKFLOW_MODE=file-agent \
CLAIMS_SILVER_FILE_AGENT_HARNESS=hermes-cli \
CLAIMS_SILVER_FILE_AGENT_COMPARISON_MODEL=deepseek/deepseek-v4-flash \
CLAIMS_SILVER_FILE_AGENT_CANONICALIZATION_MODEL=deepseek/deepseek-v4-flash \
CLAIMS_SILVER_FILE_AGENT_CANONICAL_AUDIT_MODEL=qwen/qwen3.7-flash \
CLAIMS_SILVER_FILE_AGENT_MAX_TURNS=30 \
CLAIMS_SILVER_FILE_AGENT_MAX_TOKENS=32768 \
CLAIMS_SILVER_FILE_AGENT_REQUIRE_DISTINCT_JUDGES=true \
python -m neurons.validator ...
```

### Local Live Benchmark

Run Rietveld once through the `pdf-inspector` miner, reuse that artifact across
Hermes, Codex, and Claude file-agent workflows, and persist each run to a local
SQLite backend:

```bash
conda run -n claims_subnet python scripts/live_file_agent_benchmark.py \
  --output-root outputs/file_agent_benchmark/live \
  --harnesses hermes-cli,codex-cli,claude-cli
```

Add `--reuse-miner` to retry only the validator harnesses. Add `--local-stub`
for an offline subprocess and backend-contract smoke test. Results, workspaces,
the SQLite database, and `benchmark_summary.json` stay under `--output-root`.
Live diagnostic validation runs once through Hermes/OpenRouter and is reused by
all harness comparisons; use `--diagnostic-mode deterministic` only for smoke tests.
The live mode sends the paper text, claims, and evidence to the configured model
providers; run it only for material you are authorized to share.
File-agent CLI usage is recovered from Hermes, Codex, and Claude session data
when an early valid-output exit omits the normal footer. Set `CLAIMS_MODEL_PRICING_JSON`
only when a subscription CLI exposes tokens but no per-run USD charge.

## Runtime Controls

- `--max-agent-iters` or `SUBNET_CLAIMS_VALIDATOR_AGENT_MAX_ITERS`: native rigor
  agent loop budget.
- `SUBNET_CLAIMS_VALIDATOR_AGENT_TIMEOUT`: subprocess timeout for `agent-cli`.
- `--threshold`: final pass threshold, default `0.7`.

If the rigor backend fails, the validator writes a controlled critical finding
with `metadata.code = "rigor_agent_failed"` and still emits
`agent_v1_validation_report.json`.
