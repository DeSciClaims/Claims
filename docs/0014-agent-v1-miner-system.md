# agent_v1 Miner System

`miner.agent_v1` is the canonical Claims miner. It compiles one source document
into a strict Claims Agent artifact at `agent_output.json`.

The miner is skill-driven: it mounts a compiler skill, gives the agent a run
directory with source payload and schema files, then validates and repairs the
returned artifact.

## Inputs

Exactly one source input is used:

- `--pdf <path>`
- `--text <path>`
- `--artifact-json <path>`

The runner converts that input into:

- `paper.json`: normalized paper metadata.
- `source_payload.json`: source spans and source metadata available to the agent.
- `agent_schema.json`: JSON schema for `agent_output.json`.
- `output_contract.json`: compact contract pointer.
- `request.json`: file paths and task envelope for the backend runtime.
- `validation_feedback.json`: deterministic repair feedback from the previous attempt.
- `skill_manifest.json`: mounted skill resources and hashes.

## Output

The required miner output is `agent_output.json`:

```json
{
  "ara_version": "1.0",
  "paper": {
    "paper_id": "...",
    "title": "...",
    "authors": [],
    "year": null,
    "venue": null,
    "doi": null,
    "domain": null,
    "keywords": [],
    "abstract": "...",
    "claims_summary": []
  },
  "logic": {
    "problem_observations": [],
    "gaps": [],
    "key_insight": "...",
    "assumptions": [],
    "claims": [],
    "concepts": [],
    "experiments": [],
    "related_work": [],
    "constraints": []
  },
  "evidence": {
    "records": [],
    "ledger_notes": []
  },
  "trace": {
    "node_id": "Q0",
    "node_type": "question",
    "support_level": "inferred",
    "summary": "...",
    "source_refs": [],
    "evidence": [],
    "children": []
  },
  "src": {
    "environment": [],
    "artifacts": []
  },
  "metadata": {}
}
```

The runner also writes:

- `agent_validation_report.json`: deterministic miner-side schema/reference issues.
- `PAPER.md`: readable summary export.
- `backend_manifest.json`: runtime, elapsed time, usage, and skill metadata.
- `backend_stdout.txt` and `backend_stderr.txt`.

## Core Object Rules

- `logic.claims[].claim_id` must be unique.
- `logic.claims[].statement`, `conditions`, and `falsification_criteria` must be populated.
- `logic.claims[].proof[]` must reference `logic.experiments[].experiment_id`.
- `logic.claims[].evidence_ids[]` must reference `evidence.records[].evidence_id`.
- `logic.experiments[].verifies[]` must reference `logic.claims[].claim_id`.
- `evidence.records[].linked_claim_ids[]` must reference `logic.claims[].claim_id`.
- `trace.evidence[]` must reference claim IDs.
- Source refs should use source span IDs from `source_payload.json` and preserve exact quotes when possible.

## SourceRef Shape

Claims store source refs in `logic.claims[].sources[]`. Evidence, concepts,
experiments, and trace nodes use `source_refs[]`.

```json
{
  "source_id": "S01",
  "source_type": "span",
  "path": null,
  "span_ids": ["paper-span-0001"],
  "quote": "exact source quote",
  "role": "result"
}
```

Allowed roles are `input`, `result`, `method`, `interpretation`, and `metadata`.

## Runtimes

Supported miner runtimes:

- `dspy-react`
- `langchain-agent`
- `agent-cli`

Important controls:

- `--max-agent-iters` or `SUBNET_CLAIMS_AGENT_MAX_ITERS`: native agent loop budget.
- `SUBNET_CLAIMS_AGENT_MAX_REPAIR_ATTEMPTS`: outer compile/repair attempts.
- `SUBNET_CLAIMS_AGENT_MODEL`: model name.
- `SUBNET_CLAIMS_AGENT_SKILL_DIR`: compiler skill directory.
- `SUBNET_CLAIMS_AGENT_CLI_COMMAND`: external runtime wrapper for `agent-cli`.

## Skill Use

The default compiler skill is vendored from the upstream ARA compiler skill at:

```text
miner/agent_v1/skills/compiler
```

Claims adds a local JSON output contract so the agent emits `agent_output.json`
instead of only an ARA markdown directory.

