# Agent V1 Miner

`agent_v1` is the canonical Claims miner pipeline. It uses the canonical
[ARA](https://github.com/ARA-Labs/Agent-Native-Research-Artifact) `compiler`
skill from `Agent-Native-Research-Artifact/skills/compiler` as its first target
skill/schema, but the pipeline boundary is intentionally agent-shaped rather
than model-call-shaped.

The runner owns:

- paper ingestion
- per-task run directories
- skill package loading and hashing
- runtime invocation
- output validation and one repair attempt
- ARA markdown materialization
- runtime metrics aggregation

Agent runtimes own:

- model/provider setup
- agent loop behavior
- tool use
- skill interpretation
- producing `agent_output.json`

## Runtimes

Supported runtime names:

- `dspy-react`
- `langchain-agent`
- `agent-cli`

`dspy-react` and `langchain-agent` adapt the mounted SkillPack into native Python
agent instructions and tools. `agent-cli` is the compatibility path for external
loops such as Codex, Claude, or Hermes wrappers.

## Run

```bash
python -m miner.agent_v1 \
  --text /path/to/paper.txt \
  --runtime dspy-react \
  --output-dir /tmp/paper_agent_v1
```

Alternative inputs:

```bash
python -m miner.agent_v1 --pdf /path/to/paper.pdf --pdf-reader pdf-inspector --output-dir /tmp/paper_agent_v1
python -m miner.agent_v1 --artifact-json /path/to/artifact.json --output-dir /tmp/paper_agent_v1
```

PDF inputs use `pdf-inspector` by default. Use `--pdf-reader pypdf` for the
simple PyPDF path or `--pdf-reader grobid` when you want GROBID/TEI extraction.

## CLI Runtime

Set `SUBNET_CLAIMS_AGENT_CLI_COMMAND` to a wrapper command. The runner appends:

```text
--run-dir <run_dir>
--skill-dir <skill_dir>
--request <run_dir/request.json>
--output <run_dir/agent_output.json>
```

The wrapper should write `agent_output.json`. The miner writes
`agent_schema.json`, `output_contract.json`, `backend_stdout.txt`,
`backend_stderr.txt`, and `backend_manifest.json`.

## Neuron Runtime

`agent_v1` is the default miner neuron pipeline. The `--claims.pipeline
agent_v1` flag is accepted for explicitness, but is not required.

```bash
python -m neurons.miner \
  --netuid <NETUID> \
  --wallet.name <MINER_WALLET> \
  --wallet.hotkey <HOTKEY> \
  --subtensor.network <NETWORK> \
  --claims.agent-harness hermes-cli \
  --claims.agent-model openai/gpt-5-mini \
  --claims.pdf-extraction-method pdf-inspector \
  --claims.output-dir miner/agent_v1/outputs/neuron
```

Useful flags:

- `--claims.agent-skill-dir`: override the mounted SkillPack.
- `--claims.agent-timeout`: runtime timeout in seconds.
- `--claims.agent-max-extraction-source-chars`: source text budget for native DSPy/LangChain extraction prompts. File-agent harnesses such as Hermes receive the complete `source_payload.json` path and can inspect it incrementally. The former `--claims.agent-max-source-chars` name remains as a compatibility alias.
- `--claims.agent-max-iters`: native agent loop iteration budget.
- `--claims.pdf-extraction-method`: choose `pdf-inspector`, `pypdf`, or `grobid`.

### V1 Consensus Review

The existing miner Axon accepts consensus review as a second task type. No
second miner process, hotkey, Axon port, or registration is required. Production
review assignments contain shuffled claim-comparison cases, including hidden
test cases with known answers. Cases are grouped by paper so each source is
downloaded and extracted once.

For every case, the miner selects one listed option and returns confidence, a
short rationale, and one to four verbatim evidence quotes. The backend matches
those quotes against Bronze; miner-local span IDs are informative but are not
trusted. Completed responses are signed and uploaded to the miner-upload API,
while Dendrite returns only the durable submission manifest.

Consensus review is not paid separately. Its hidden-case score gates future
extraction eligibility. Never-reviewed miners are provisional; after the first
review, the exact mean of up to the latest three scores must remain at least
`0.75`. A normal non-response scores zero.

- Exhausted review retries return `review_status=failed` with no vote or evidence.
- Failed test-case answers remain in the scoring denominator and earn zero.
- `insufficient_information` is reserved for an evidence-based judgment, not a technical failure.

#### Setup

- Update the existing miner checkout and dependencies from `main`.
- Keep `CLAIMS_BACKEND_URL=https://artifacts.claims111.ai`.
- Set `SUBNET_CLAIMS_CONSENSUS_MODE=model`.
- Configure the optional consensus provider/model overrides below, or omit them
  to inherit the extraction provider and model.
- Restart the existing miner process with its updated environment.

```env
SUBNET_CLAIMS_CONSENSUS_MODE=model
# Optional overrides; otherwise consensus inherits SUBNET_CLAIMS_AGENT_*.
SUBNET_CLAIMS_CONSENSUS_PROVIDER=openrouter
SUBNET_CLAIMS_CONSENSUS_MODEL=deepseek/deepseek-v4-flash
SUBNET_CLAIMS_CONSENSUS_API_BASE=https://openrouter.ai/api/v1
SUBNET_CLAIMS_CONSENSUS_API_KEY_ENV=OPENROUTER_API_KEY
SUBNET_CLAIMS_CONSENSUS_MAX_TOKENS=8192
SUBNET_CLAIMS_CONSENSUS_TIMEOUT=1800
SUBNET_CLAIMS_CONSENSUS_BATCH_SIZE=2
SUBNET_CLAIMS_CONSENSUS_MAX_WORKERS=4
SUBNET_CLAIMS_CONSENSUS_SOURCE_MAX_WORKERS=4
```

The reviewer uses structured DSPy calls. OpenRouter and Chutes may be selected
independently for extraction and consensus.
`SUBNET_CLAIMS_CONSENSUS_SOURCE_MAX_WORKERS` bounds concurrent PDF downloads
and source-payload extraction independently from concurrent model batches.
`compatibility` mode exists for protocol tests only and should not be used for
scored consensus work.

## Runtime Metrics

Successful runs attach runtime metadata to `agent_output.json`:

```json
{
  "metadata": {
    "runtime": "dspy-react",
    "runtime_metrics": {
      "elapsed_seconds": 181.757,
      "attempt_count": 2,
      "models": ["openrouter/openai/gpt-4o-mini"],
      "token_usage": {
        "prompt_tokens": 412995,
        "completion_tokens": 6465,
        "total_tokens": 419460
      },
      "cost_usd": 0.04276905,
      "usage_source": "dspy_lm_history"
    }
  }
}
```

Backend usage support is best-effort because different agent loops expose
different telemetry:

- DSPy: token usage and provider cost when present in LM history.
- LangChain: token usage from message usage metadata; cost is provider-dependent.
- Codex CLI: token usage from `codex exec --json`; cost is not emitted.
- Hermes CLI: token usage and cost from `hermes sessions export`.
- Claude CLI: elapsed time only by default.
- Generic CLI: elapsed time only unless the wrapper output matches a known
  telemetry source.

## Skill Preservation

Skills are loaded as full packages, not flattened into only tool descriptions.
`SKILL.md` becomes the top-level instructions, every mounted resource is hashed,
and the skill manifest is written into the run directory. Native runtimes receive
the same instructions/resources/tools contract; external CLIs receive the same
skill directory and run files.

The default mounted skill is:

```text
miner/agent_v1/skills/compiler/
```

It mirrors the canonical ARA compiler skill and includes its reference files:

- `references/ara-schema.md`
- `references/exploration-tree-spec.md`
- `references/figure-extraction-guide.md`
- `references/validation-checklist.md`

Claims also adds `references/claims-agent-v1-json-output-contract.md` so runtimes
know that this miner expects the final structured `agent_output.json` payload.
Each run also writes a generated `agent_schema.json` from the Pydantic
`Artifact` model; native runtimes can read it with `read_output_schema`.
