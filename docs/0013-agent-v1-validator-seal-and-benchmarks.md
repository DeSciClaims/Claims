# Agent v1 Validator, ARA Seal, and Benchmarks

This document describes how Claims should adopt the
[ARA Seal](https://github.com/ARA-Labs/Agent-Native-Research-Artifact) process
for `validator.agent_v1`, what Claims adds on top, and how we should benchmark
miners, validator agents, models, and agent backends.

The core decision is:

```text
agent_v1 miner output requires agent_v1 validator scoring
```

`validator.v0` remains useful for legacy flat claim-evidence outputs, but it is
not the right validator for ARA-shaped miner responses. ARA artifacts carry
logic, evidence, trace, source refs, experiments, and runtime metadata. The
validator needs to read and score that richer structure.

## Design Goal

`validator.agent_v1` should be a Claims-aware ARA Seal validator:

```text
validator.agent_v1 =
  ARA Seal Level 1 adapted to Claims JSON
  + source-payload grounding checks
  + required ARA Seal Level 2-style rigor agent
  + deterministic subnet scoring from findings
  + benchmark hooks for miner/backend evaluation
```

The validator should not be only a schema checker, and it should not be only an
LLM judge. It should combine deterministic checks with an agentic rigor review.
The rigor review is required for production scoring, but the final numeric score
is computed by code from structured findings.

## What ARA Seal Provides

The ARA paper defines the Seal as a machine-verifiable research credential with
three escalating levels:

| Level | Name | Purpose | Cost profile |
| --- | --- | --- | --- |
| Level 1 | Structural Integrity | Validate schema, required fields, files/layers, and cross-layer references | deterministic, seconds |
| Level 2 | Argumentative Rigor | Agentic semantic review of claims, evidence, scope, methods, and argument quality | agentic, minutes |
| Level 3 | Execution Reproducibility | Coding-agent reproduction or directional tests of key claims | hours to days |

The ARA repo contains directly relevant implementation material:

- `skills/compiler/references/validation-checklist.md`: Level 1 structural checklist.
- `skills/rigor-reviewer/SKILL.md`: Level 2 semantic rigor reviewer.
- `docs/the-ara-of-ara/src/seal/seal.py`: reference Seal implementation.
- `docs/the-ara-of-ara/src/eval/`: benchmark harnesses for QA, reproduction,
  extension, and mutation-style review evaluation.

For Claims, the most important reusable idea is not a file-for-file copy of the
Seal implementation. The reusable idea is the pipeline:

```text
structural validity -> semantic rigor findings -> deterministic verdict
```

## What Claims Adds

Claims has validation inputs that a published ARA Seal reviewer may not have:

```text
agent_output.json
source_payload.json
backend_manifest.json
validation_report.json
miner runtime metadata
validator task metadata
```

This lets us do stricter checks than generic artifact review. ARA Seal Level 2
intentionally reviews the artifact alone and does not consult external sources.
That is appropriate for publication readiness. Claims validators, however,
receive the source spans used for the miner task. We can check whether every
quote and span reference actually grounds the artifact.

Claims-specific additions:

- source span resolution against `source_payload.json`
- quote matching against referenced spans
- numerical grounding checks for claim/evidence statements
- provenance checks for source roles and source paths
- miner runtime metadata capture
- subnet scoring and pass thresholds
- compatibility with a JSON ARA projection instead of only a markdown ARA directory

## Required Validator Pipeline

Every production `validator.agent_v1` run should include all three passes:

```text
Pass 1: deterministic structural checks
Pass 2: deterministic source-grounding checks
Pass 3: required agentic rigor review
Pass 4: deterministic score aggregation
```

The rigor review is not optional for production scoring. The only acceptable
short-circuit is malformed output that cannot be reviewed safely, for example
invalid JSON or missing top-level artifact layers. In that case the validator
should emit blocker findings and skip the agent pass to avoid wasting validator
compute.

For local development we can expose a debug flag such as `--skip-rigor-agent`,
but that should not be used in subnet scoring.

## Pass 1: Structural Checks

Structural checks are deterministic. They adapt ARA Seal Level 1 to our JSON
schema.

Inputs:

```text
agent_output.json
agent_schema.json, when available
```

Checks:

- JSON parses.
- Pydantic `Artifact` validation passes.
- Required top-level keys exist: `paper`, `logic`, `evidence`, `trace`, `src`,
  `metadata`.
- IDs are unique within their layer:
  - `logic.claims[].claim_id`
  - `logic.concepts[].concept_id`
  - `logic.experiments[].experiment_id`
  - `evidence.records[].evidence_id`
  - trace node IDs
- Claim references resolve:
  - `claim.evidence_ids[]` exists in `evidence.records[]`
  - `claim.proof[]` exists in `logic.experiments[]`
  - `claim.dependencies[]` exists in `logic.claims[]`
- Experiment references resolve:
  - `experiment.verifies[]` exists in `logic.claims[]`
  - `experiment.evidence_ids[]` exists in `evidence.records[]`
- Evidence references resolve:
  - `evidence.linked_claim_ids[]` exists in `logic.claims[]`
- Trace references resolve:
  - trace `evidence[]` claim IDs exist
  - child nodes are well-formed
  - `support_level` is valid
- Required fields are non-empty enough to review:
  - claim `statement`
  - claim `conditions`
  - claim `falsification_criteria`
  - evidence `summary`
  - experiment `setup`, `procedure`, `expected_outcome`

Structural findings should be blocker or major severity when they prevent
review, and minor severity when the artifact is reviewable but incomplete.

## Pass 2: Source-Grounding Checks

Grounding checks are Claims-specific and should be deterministic as much as
possible.

Inputs:

```text
agent_output.json
source_payload.json
```

Checks:

- Every `source_ref.span_ids[]` exists in `source_payload.spans[]`.
- Every non-empty `source_ref.quote` appears in the referenced span text.
- Quote matching uses normalized whitespace and punctuation-tolerant matching.
- Source refs use valid roles:
  - `input`
  - `result`
  - `method`
  - `interpretation`
  - `metadata`
- Every claim has either direct source refs or evidence refs that lead to source refs.
- Every evidence record has at least one source ref unless explicitly marked
  unavailable from provided input.
- Load-bearing numbers in claim statements, claim conditions, evidence summaries,
  and experiment descriptions appear in a quoted source ref or referenced span.
- Evidence records should not cite spans that contradict their summaries.

The numerical grounding check does not need to solve all numeric semantics in
the MVP. A practical first version can extract numeric strings and require them
to appear somewhere in source refs connected to the same claim or evidence
record. Later versions can normalize percentages, commas, scientific notation,
and approximate values.

Grounding failures are especially important for subnet scoring because they are
anti-hallucination checks. A miner that produces elegant ARA structure with
unsupported source refs should score poorly.

## Pass 3: Required Rigor Agent

The rigor pass should run as an agent, just like the miner backends. It should
use a Claims-mounted rigor reviewer skill derived from ARA `rigor-reviewer`.

The validator agent receives:

```text
agent_output.json
source_payload.json
structural_findings.json
grounding_findings.json
rigor_review_schema.json
rigor skill instructions
```

It emits structured findings, not a final score.

Required dimensions:

| Dimension | Checks |
| --- | --- |
| Evidence relevance | Does cited evidence actually support each claim? |
| Falsifiability quality | Are falsification criteria specific, actionable, and scoped? |
| Scope calibration | Do claims assert only what evidence supports? |
| Argument coherence | Do problem, insight, claims, experiments, and evidence tell a consistent story? |
| Exploration integrity | Does the trace honestly represent decisions, failures, uncertainty, or lack of available exploration detail? |
| Methodological rigor | Are methods, baselines, ablations, statistics, and metrics adequate for the claims? |

The agent must quote exact artifact spans when reporting a finding. For absences,
it may omit the quote but must identify the missing target. It should never fetch
external sources. It should not execute code. It should not assign the final
subnet score.

### Validator Backends

The validator should mirror the miner backend strategy:

```text
dspy-react validator runtime
langchain-agent validator runtime
agent-cli validator runtime
  codex wrapper
  hermes wrapper
  claude wrapper
```

This lets validators choose an agent loop while preserving a single contract:

```text
input files -> rigor findings JSON -> deterministic scoring
```

### Why The Agent Produces Findings, Not Scores

The ARA paper reports a failure mode in Level 2 auditing: the reviewer agent can
correctly identify a severe issue while still assigning an inflated dimension
score. To avoid that, Claims should separate responsibilities:

```text
agent: find and explain issues
code: compute score from issues
```

This keeps the validator more stable and harder to game.

## Pass 4: Deterministic Scoring

The score is computed from findings across all passes.

A first scoring policy can be simple:

```text
start score = 1.0

critical finding: -0.25
major finding:    -0.10
minor finding:    -0.03
warning finding:  -0.01

floor rules:
  invalid JSON                       -> max 0.05
  schema/Pydantic parse failure       -> max 0.20
  missing logic/evidence layer        -> max 0.25
  no evidence records                 -> max 0.40
  no source refs                      -> max 0.50
  any critical grounding failure      -> max 0.50
  rigor agent unavailable in prod     -> max 0.60
```

Pass decision:

```text
passed = score >= threshold and no blocker finding
```

The final report should include both the score and the findings so miners can
debug, validators can audit, and benchmark runners can aggregate failure modes.

## Report Schema

Proposed output:

```json
{
  "validator_version": "agent_v1",
  "artifact_path": "outputs/run/agent_output.json",
  "source_payload_path": "outputs/run/source_payload.json",
  "paper_id": "paper-id",
  "passed": true,
  "score": 0.87,
  "threshold": 0.70,
  "summary": {
    "critical": 0,
    "major": 1,
    "minor": 3,
    "warning": 2
  },
  "passes": {
    "structural": {
      "passed": true,
      "finding_count": 0
    },
    "grounding": {
      "passed": true,
      "finding_count": 2
    },
    "rigor": {
      "passed": true,
      "runtime": "agent-cli",
      "finding_count": 4
    }
  },
  "findings": [
    {
      "finding_id": "F001",
      "pass_name": "grounding",
      "dimension": "source_quote_grounding",
      "severity": "major",
      "target_type": "evidence",
      "target_id": "EV03",
      "message": "The evidence quote does not appear in the referenced source span.",
      "evidence_span": "reported quote",
      "suggestion": "Replace the quote with an exact excerpt from the referenced span or change the source ref."
    }
  ],
  "metrics": {
    "elapsed_seconds": 81.4,
    "rigor_agent_elapsed_seconds": 63.2,
    "token_usage": {
      "prompt_tokens": 100000,
      "completion_tokens": 3000,
      "total_tokens": 103000
    },
    "cost_usd": 0.08
  }
}
```

## Proposed Package Layout

```text
validator/agent_v1/
├── __init__.py
├── __main__.py
├── config.py
├── models.py
├── runner.py
├── structural.py
├── grounding.py
├── scoring.py
├── runtime/
│   ├── base.py
│   ├── dspy_react.py
│   ├── langchain_agent.py
│   └── subprocess_cli.py
├── wrappers/
│   ├── codex_prompt.py
│   ├── hermes_prompt.py
│   └── claude_prompt.py
└── skills/
    └── rigor_reviewer/
        ├── SKILL.md
        └── references/
            └── claims-agent-v1-rigor-output-contract.md
```

The CLI should look like:

```bash
python -m validator.agent_v1 \
  --agent-json outputs/rietveld_agent_v1_dspy/agent_output.json \
  --source-payload outputs/rietveld_agent_v1_dspy/source_payload.json \
  --runtime dspy-react \
  --output-dir validator/agent_v1/outputs/rietveld_dspy
```

External validator agent example:

```bash
SUBNET_CLAIMS_VALIDATOR_AGENT_CLI_COMMAND=".venv/bin/python -m validator.agent_v1.wrappers.codex_prompt" \
python -m validator.agent_v1 \
  --agent-json outputs/rietveld_agent_v1_codex/agent_output.json \
  --source-payload outputs/rietveld_agent_v1_codex/source_payload.json \
  --runtime agent-cli \
  --output-dir validator/agent_v1/outputs/rietveld_codex
```

## Neuron Integration

The network validator should support both old and new response shapes during
migration.

Recommended logic:

```text
if extraction has keys: paper, logic, evidence, trace, src
  use validator.agent_v1
else
  use validator.v0 compatibility path
```

Eventually, when the network fully expects ARA responses, `validator.agent_v1`
should become the default validator path just as `miner.agent_v1` is the default
miner path.

## Benchmarking Plan

Benchmarking should come after the validator MVP, but the validator should be
designed so benchmarks can reuse it.

The ARA paper evaluates across three main research-use layers:

1. **Understanding:** question answering over artifacts.
2. **Reproduction:** agents reproduce paper tasks from the artifact.
3. **Extension:** agents build on prior work, especially using failure knowledge.

It also evaluates Seal effectiveness:

4. **Level 1 convergence:** generate, validate, fix until artifacts pass
   structural checks.
5. **Level 2 mutation benchmark:** inject known errors and measure rigor-auditor
   detection.

Claims should adopt the same families of benchmarks, but stage them carefully.

## Benchmark 1: Backend Matrix Validation

Purpose: compare miner backends and models on the same source papers.

Inputs:

```text
paper.pdf
agent_v1 backend config
validator.agent_v1 config
```

Matrix:

```text
miner runtime:
  dspy-react
  langchain-agent
  codex-cli
  hermes-cli
  claude-cli

validator runtime:
  dspy-react
  langchain-agent
  codex-cli
  hermes-cli
  claude-cli
```

Metrics:

- schema pass rate
- source grounding pass rate
- rigor finding counts by dimension
- final validator score
- miner elapsed time
- validator elapsed time
- miner cost
- validator cost
- repair attempts
- token usage

This is the first benchmark we should build because we already have outputs
from Rietveld and smoke runs across several backends.

## Benchmark 2: PaperBench Understanding

Purpose: test whether generated ARAs preserve information that agents need to
answer detailed research questions.

The ARA paper used PaperBench because its expert rubrics contain reproduction
requirements that PDFs often omit. Claims can use a smaller version first.

Pipeline:

```text
PaperBench paper + optional repo/rubric
  -> miner.agent_v1 backend
  -> agent_output.json
  -> question-answering agent using only ARA
  -> judge against gold/rubric-derived answer
```

Question categories:

- paper fidelity: methods, results, conditions
- configuration recovery: hyperparameters, preprocessing, environment
- implementation detail: code-level or protocol-level details when available

Metrics:

- answer accuracy
- partial-credit answer accuracy
- refusal or unanswerable rate
- hallucinated answer rate
- tokens per question
- cost per question
- source citation quality

This benchmark should not be part of live subnet validation. It is an offline
leaderboard and regression suite for miner quality.

## Benchmark 3: PaperBench Rubric Coverage

Purpose: measure whether an ARA contains the details in expert reproduction
rubrics.

Pipeline:

```text
PaperBench rubric leaf requirement
  -> search/check generated ARA
  -> classify covered / partially covered / missing
```

This can be implemented with a hybrid approach:

- deterministic retrieval over ARA fields
- agent judge for semantic coverage
- strict requirement that the judge cite artifact text

Metrics:

- coverage percentage
- weighted coverage percentage, if rubric weights are available
- missing critical requirements
- coverage by category, such as dataset, hyperparameter, model architecture,
  evaluation protocol, result, and implementation trick

This directly tests the promise of ARA as a reproduction-friendly artifact.

## Benchmark 4: Mutation Benchmark For Validator Recall

Purpose: test whether `validator.agent_v1` catches known defects.

This mirrors the ARA paper's Level 2 mutation benchmark.

Start with clean generated ARAs, then inject one defect at a time:

- unsupported claim
- missing falsification criteria
- claim evidence ID points to missing evidence
- experiment verifies missing claim
- source quote does not appear in span
- source span ID does not exist
- over-broadened claim scope
- evidence summary contradicts quote
- trace node supports a claim contradicted by evidence

Run `validator.agent_v1` blind to the injection manifest.

Metrics:

- per-defect recall
- false positive rate on clean artifacts
- severity calibration
- score impact by defect type
- validator cost
- validator runtime

This is the most important benchmark for validator trust.

## Benchmark 5: RE-Bench Style Extension

Purpose: test whether ARAs help agents build on previous work, especially when
failure trajectories and implementation details are included.

This is expensive and should come later.

Pipeline:

```text
task baseline artifact
  -> ARA or paper/repo baseline
  -> coding agent attempts improvement
  -> task scorer computes objective score
```

Metrics:

- best score achieved
- time to first valid score
- cost to reach baseline
- cost to exceed reference
- number of score attempts
- failure modes avoided

This is not necessary for the initial subnet validator, but it is useful for
proving that high-scoring Claims ARAs are not only well-formatted, but actually
useful for downstream research agents.

## Benchmark Outputs

Benchmark runs should produce machine-readable reports:

```text
benchmarks/
├── runs/<run_id>/
│   ├── matrix.json
│   ├── miner_outputs/
│   ├── validator_reports/
│   ├── qa_answers/
│   ├── judge_reports/
│   └── summary.csv
└── reports/
    ├── backend_leaderboard.csv
    ├── mutation_recall.csv
    └── paperbench_coverage.csv
```

A backend leaderboard should include:

| Backend | Model | Schema | Grounding | Rigor | QA | Cost | Time |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| DSPy | gpt-4o-mini | 0.95 | 0.88 | 0.82 | 0.76 | 0.04 | 180s |
| Codex CLI | default | 0.98 | 0.91 | 0.86 | 0.81 | n/a | 115s |
| Hermes CLI | gpt-4o-mini | 0.93 | 0.84 | 0.80 | 0.74 | 0.04 | 190s |

The exact scoring formulas can evolve, but benchmark outputs should always
include raw findings and raw metrics so later analysis can recompute scores.

## Implementation Roadmap

### Phase 1: Deterministic Validator Core

- Add `validator/agent_v1/models.py`.
- Add structural checks.
- Add grounding checks.
- Add deterministic scoring.
- Add CLI.
- Test against current `outputs/rietveld_agent_v1_*`.

### Phase 2: Required Rigor Agent

- Mount a Claims-adapted `rigor_reviewer` skill.
- Add validator runtimes mirroring miner runtimes.
- Require rigor findings in production validation.
- Track validator runtime metrics.

### Phase 3: Neuron Integration

- Route ARA-shaped miner responses to `validator.agent_v1`.
- Keep v0 fallback for legacy miner responses.
- Emit network scoring records that include validator findings.

### Phase 4: Mutation Benchmark

- Build defect injectors for JSON ARA artifacts.
- Run validator blind.
- Report recall and false positives.

### Phase 5: PaperBench Understanding And Coverage

- Add a small PaperBench subset.
- Generate or load rubric-derived questions.
- Score ARA answers against gold references.
- Produce backend/model leaderboard.

### Phase 6: Reproduction And Extension Benchmarks

- Add selected tasks with manageable compute.
- Evaluate whether higher validator scores correlate with downstream agent
  usefulness.

## Open Questions

- Should the live subnet validator always use the same rigor backend, or should
  validators be free to choose their own agent runtime?
- How much cost should validators be expected to spend per task?
- Should miner runtime cost affect score, or only leaderboard economics?
- What is the minimum artifact quality threshold before a rigor agent run is
  worthwhile?
- How should we prevent validators from overfitting to known mutation tests?
- Should PaperBench coverage become an official offline benchmark before we add
  ARA-native validator scoring to the network?

## Bottom Line

Claims should adopt ARA Seal as the validation philosophy, but implement a
Claims-aware validator rather than copying Seal verbatim.

The production validator should be:

```text
deterministic where possible
agentic where semantic rigor is required
finding-based throughout
deterministically scored
grounded against the original source payload
benchmarked against PaperBench-style QA, rubric coverage, mutation tests, and eventually RE-Bench-style extension tasks
```

That gives us a validator that matches the new `agent_v1` miner, preserves the
best ideas from ARA Seal, and adds the source-grounding and scoring machinery a
subnet needs.
