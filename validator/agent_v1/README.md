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

Set `CLAIMS_SILVER_WORKFLOW_MODE=file-agent` to run the experimental per-paper
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
