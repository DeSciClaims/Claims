# Agent V1 CLI Wrappers

`agent_v1` can call external agent loops through the `agent-cli` runtime. A
wrapper command receives a stable file contract and must write the final
structured artifact.

The miner appends these arguments:

```text
--run-dir <run_dir>
--skill-dir <skill_dir>
--request <run_dir/request.json>
--output <run_dir/agent_output.json>
```

The run directory contains:

```text
request.json
paper.json
source_payload.json
agent_schema.json
output_contract.json
validation_feedback.json
skill_manifest.json
```

The wrapper must:

1. Load `request.json`.
2. Mount or read the SkillPack at `--skill-dir`.
3. Read `source_payload.json` and `agent_schema.json`.
4. Run the external agent loop.
5. Write strict JSON to `--output`.
6. Exit nonzero if the agent fails.

The wrapper may write additional files inside `--run-dir`. The miner captures
wrapper stdout/stderr and writes `backend_manifest.json`.

## Example

```bash
SUBNET_CLAIMS_AGENT_CLI_COMMAND="python -m miner.agent_v1.wrappers.echo_contract" \
python -m miner.agent_v1 \
  --text /path/to/paper.txt \
  --runtime agent-cli \
  --output-dir /tmp/agent_v1_smoke
```

`echo_contract` is only a deterministic contract smoke wrapper. It is not a real
compiler.

## Generic Prompt Wrapper

`prompt_agent` adapts external CLIs that accept a prompt as an argument or stdin.
The outer miner command calls the wrapper; the wrapper calls the real agent.

Example for a Hermes one-shot style command:

```bash
CLAIMS_AGENT_INNER_COMMAND="../hermes-agent/hermes --oneshot --toolsets development" \
SUBNET_CLAIMS_AGENT_CLI_COMMAND=".venv/bin/python -m miner.agent_v1.wrappers.prompt_agent" \
python -m miner.agent_v1 \
  --text /path/to/paper.txt \
  --runtime agent-cli \
  --output-dir /tmp/paper_agent_v1
```

By default the generated prompt is appended as the last argv item. Set
`CLAIMS_AGENT_PROMPT_MODE=stdin` for CLIs that read the prompt from stdin.

If the inner agent writes `agent_output.json`, the wrapper exits successfully.
If not, the wrapper tries to extract a JSON object from stdout and writes it to
the expected output path.

## Hermes Convenience Wrapper

`hermes_prompt` is a thin shim over `prompt_agent`. If
`CLAIMS_AGENT_INNER_COMMAND` is not set, it tries `hermes` on PATH, then the
sibling workspace path `../hermes-agent/hermes`, and calls it with `chat -q`.

```bash
SUBNET_CLAIMS_AGENT_CLI_COMMAND=".venv/bin/python -m miner.agent_v1.wrappers.hermes_prompt" \
python -m miner.agent_v1 \
  --text /path/to/paper.txt \
  --runtime agent-cli \
  --output-dir /tmp/paper_agent_v1
```

Override the inner command when needed:

```bash
CLAIMS_AGENT_INNER_COMMAND="../hermes-agent/hermes --oneshot --toolsets development" \
SUBNET_CLAIMS_AGENT_CLI_COMMAND=".venv/bin/python -m miner.agent_v1.wrappers.hermes_prompt" \
python -m miner.agent_v1 --text /path/to/paper.txt --runtime agent-cli --output-dir /tmp/paper_agent_v1
```

## Codex Convenience Wrapper

`codex_prompt` is a thin shim over `prompt_agent`. If
`CLAIMS_AGENT_INNER_COMMAND` is not set, it finds `codex` on PATH and calls:

```text
codex exec --sandbox workspace-write --skip-git-repo-check <generated prompt>
```

```bash
SUBNET_CLAIMS_AGENT_CLI_COMMAND=".venv/bin/python -m miner.agent_v1.wrappers.codex_prompt" \
python -m miner.agent_v1 \
  --text /path/to/paper.txt \
  --runtime agent-cli \
  --output-dir /tmp/paper_agent_v1_codex
```
