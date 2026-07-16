# validator.agent_v1

`validator.agent_v1` validates canonical `miner.agent_v1` agent JSON outputs. It
uses deterministic checks for schema, cross references, and source grounding,
then runs a required agent rigor pass over ARA Seal-style rigor dimensions.

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
- `grounding_findings.json`: deterministic source-ref and quote findings.
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

## Runtime Controls

- `--max-agent-iters` or `SUBNET_CLAIMS_VALIDATOR_AGENT_MAX_ITERS`: native rigor
  agent loop budget.
- `SUBNET_CLAIMS_VALIDATOR_AGENT_TIMEOUT`: subprocess timeout for `agent-cli`.
- `--threshold`: final pass threshold, default `0.7`.

If the rigor backend fails, the validator writes a controlled critical finding
with `metadata.code = "rigor_agent_failed"` and still emits
`agent_v1_validation_report.json`.
