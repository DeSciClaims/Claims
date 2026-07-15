# Agent v1 Canonical Miner

`agent_v1` is the canonical Claims miner pipeline.

It compiles source papers into structured
[ARA v1](https://github.com/ARA-Labs/Agent-Native-Research-Artifact) artifacts
with an agent loop and a mounted ARA compiler skill. The older `miner.v0`
direct model pipeline is kept as a legacy compatibility path, not the preferred
implementation path for new miners.

## Why agent_v1

The Claims task is no longer just a single structured model call. Strong miner
outputs need agent behavior:

- read source payloads and schema files deliberately
- preserve skill instructions and reference files
- use tools for file access, validation, repair, and evidence grounding
- emit a richer research artifact, not only flat claim-evidence rows
- record backend metadata such as elapsed time, attempts, tokens, and cost

`agent_v1` makes those behaviors part of the miner contract. It uses the ARA
schema as the canonical internal artifact shape:

```text
agent_output.json
├── paper
├── logic
│   ├── claims
│   ├── concepts
│   └── experiments
├── evidence
│   └── records
├── trace
├── src
└── metadata
    └── runtime_metrics
```

## What remains v0

The network protocol and validator stack still include Claims v0 compatibility:

- `neurons.tasks.PROTOCOL_VERSION` remains `claims.v0`.
- `validator.v0` still audits miner responses.
- `miner.v0` can still be run explicitly with `--claims.pipeline v0`.

That compatibility layer should not drive new miner design. New extraction work
should target ARA first, then derive legacy projections only where validators or
downstream tools still require them.

## Local agent_v1 runs

DSPy ReAct:

```bash
python -m miner.agent_v1 \
  --pdf /path/to/paper.pdf \
  --runtime dspy-react \
  --output-dir outputs/my_agent_v1_run
```

LangChain agent:

```bash
python -m miner.agent_v1 \
  --pdf /path/to/paper.pdf \
  --runtime langchain-agent \
  --output-dir outputs/my_agent_v1_run
```

Codex CLI:

```bash
SUBNET_CLAIMS_AGENT_CLI_COMMAND=".venv/bin/python -m miner.agent_v1.wrappers.codex_prompt" \
python -m miner.agent_v1 \
  --pdf /path/to/paper.pdf \
  --runtime agent-cli \
  --output-dir outputs/my_agent_v1_codex_run
```

Hermes CLI:

```bash
CLAIMS_AGENT_INNER_COMMAND="hermes chat --provider openrouter -m openai/gpt-4o-mini --max-turns 30 -q" \
SUBNET_CLAIMS_AGENT_CLI_COMMAND=".venv/bin/python -m miner.agent_v1.wrappers.hermes_prompt" \
python -m miner.agent_v1 \
  --pdf /path/to/paper.pdf \
  --runtime agent-cli \
  --output-dir outputs/my_agent_v1_hermes_run
```

## Miner neuron default

`python -m neurons.miner` defaults to `--claims.pipeline agent_v1`.

Recommended native runtime:

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

External CLI runtime:

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

Legacy v0 miner:

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

## Backend metadata

Each successful `agent_v1` run writes runtime metadata into
`agent_output.json`:

```json
{
  "metadata": {
    "runtime_metrics": {
      "elapsed_seconds": 60.911,
      "attempt_count": 1,
      "models": ["openrouter/openai/gpt-4o-mini"],
      "token_usage": {
        "prompt_tokens": 80908,
        "completion_tokens": 1974,
        "total_tokens": 82882
      },
      "cost_usd": null,
      "usage_source": "langchain_usage_metadata"
    }
  }
}
```

Usage source support:

| Runtime | Token usage | Cost |
| --- | --- | --- |
| `dspy-react` | DSPy LM history | When the provider response includes cost |
| `langchain-agent` | LangChain usage metadata | Usually unavailable unless provider metadata includes cost |
| Codex CLI | `codex exec --json` events | Not currently emitted by Codex JSONL |
| Hermes CLI | `hermes sessions export` | Yes, when Hermes records session cost |
| Generic CLI | elapsed time only | unavailable |

## Migration rule

Treat ARA as the source of truth:

```text
agent_v1 ARA artifact -> compatibility/export projections
```

Avoid adding new capabilities to v0 unless they are required to keep existing
validators or import tools working. New miner quality work should happen in
`miner/agent_v1`, the agent schema, the mounted compiler skill, or the runtime
wrappers.
