# agent_v1 Validator System

`validator.agent_v1` is the canonical validator for Claims Agent artifacts. It
validates `agent_output.json` with deterministic checks and a required semantic
rigor agent pass, then emits a stable score report.

The validator intentionally separates semantic review from scoring:

```text
structural findings
+ grounding findings
+ rigor findings
-> deterministic score
```

The rigor agent emits findings only. Python scoring code computes the final
score and pass/fail verdict.

## Inputs

Required:

- `--agent-json <path>`: miner `agent_output.json`.

Recommended:

- `--source-payload <path>`: miner `source_payload.json`.

Optional:

- `--output-dir <path>`
- `--runtime dspy-react|langchain-agent|agent-cli`
- `--max-agent-iters <n>`
- `--threshold <float>`
- `--skip-rigor-agent` for local deterministic smoke tests only.

## Output Files

Each validator run writes:

- `agent_v1_validation_report.json`: final validator report.
- `structural_findings.json`: deterministic schema and reference findings.
- `grounding_findings.json`: deterministic source-grounding findings.
- `rigor_findings.json`: semantic rigor findings from the rigor agent, or a controlled failure/skipped finding.
- `rigor_findings_schema.json`: JSON schema given to the rigor agent.
- `rigor_backend_manifest.json`: runtime metadata for the rigor backend.
- `rigor_backend_stdout.txt` and `rigor_backend_stderr.txt`.

## Final Report Schema

`agent_v1_validation_report.json` has this shape:

```json
{
  "validator_version": "agent_v1",
  "artifact_path": "/path/to/agent_output.json",
  "source_payload_path": "/path/to/source_payload.json",
  "paper_id": "paper-id",
  "passed": false,
  "score": 0.72,
  "threshold": 0.7,
  "summary": {
    "blocker": 0,
    "critical": 0,
    "major": 2,
    "minor": 1,
    "warning": 0,
    "suggestion": 0
  },
  "passes": {
    "structural": {
      "passed": true,
      "finding_count": 0,
      "runtime": "deterministic"
    },
    "grounding": {
      "passed": true,
      "finding_count": 0,
      "runtime": "deterministic"
    },
    "rigor": {
      "passed": false,
      "finding_count": 3,
      "runtime": "dspy-react"
    }
  },
  "findings": [],
  "metrics": {
    "elapsed_seconds": 12.34,
    "rigor_agent_elapsed_seconds": 11.9,
    "token_usage": {
      "prompt_tokens": null,
      "completion_tokens": null,
      "total_tokens": null
    },
    "cost_usd": null,
    "usage_source": "unavailable"
  },
  "metadata": {}
}
```

## Finding Schema

Every finding in the final report uses:

```json
{
  "finding_id": "G001",
  "pass_name": "grounding",
  "dimension": "source_quote_grounding",
  "severity": "critical",
  "target_type": "claim",
  "target_id": "C01",
  "message": "Source quote does not appear in the referenced source span text.",
  "evidence_span": "quoted text",
  "suggestion": "Replace the quote with an exact excerpt from the referenced source span.",
  "metadata": {
    "code": "quote_not_in_source"
  }
}
```

Allowed severities:

- `blocker`
- `critical`
- `major`
- `minor`
- `warning`
- `suggestion`

Allowed pass names:

- `structural`
- `grounding`
- `rigor`
- `scoring`

## Rigor Agent Output Schema

The rigor agent writes only `rigor_findings.json`:

```json
{
  "findings": [
    {
      "dimension": "scope_calibration",
      "severity": "major",
      "target_type": "claim",
      "target_id": "C01",
      "message": "The claim generalizes beyond the cited evidence.",
      "evidence_span": "exact artifact quote or null",
      "suggestion": "Narrow the claim conditions to the tested population.",
      "metadata": {}
    }
  ]
}
```

Allowed rigor dimensions:

- `evidence_relevance`
- `falsifiability_quality`
- `scope_calibration`
- `argument_coherence`
- `exploration_integrity`
- `methodological_rigor`

Allowed rigor severities:

- `critical`
- `major`
- `minor`
- `warning`
- `suggestion`

## Deterministic Passes

Structural checks cover:

- JSON parseability.
- required top-level layers.
- Pydantic `Artifact` validation.
- unique IDs.
- claim, experiment, evidence, and trace cross-reference resolution.
- empty required claim, evidence, and experiment fields.

Grounding checks cover:

- missing `source_payload.json`.
- missing span IDs.
- invalid source roles.
- source quotes that do not appear in referenced spans.
- load-bearing numbers not present in connected source refs/spans.
- claims or evidence records without source refs.

## Scoring

The score starts at `1.0` and subtracts severity penalties:

| Severity | Penalty |
| --- | ---: |
| blocker | 1.00 |
| critical | 0.25 |
| major | 0.10 |
| minor | 0.03 |
| warning | 0.01 |
| suggestion | 0.00 |

The verdict is:

```text
passed = score >= threshold and no blocker or critical findings
```

Some finding codes apply score caps. For example:

- invalid JSON or schema failure caps score near failure.
- missing evidence records cap score.
- quote/source grounding failures cap score.
- skipped rigor pass caps score at `0.60`.
- failed rigor backend caps score at `0.30`.

## Runtimes

Supported rigor runtimes:

- `dspy-react`
- `langchain-agent`
- `agent-cli`

Important controls:

- `--max-agent-iters` or `SUBNET_CLAIMS_VALIDATOR_AGENT_MAX_ITERS`: native rigor loop budget.
- `SUBNET_CLAIMS_VALIDATOR_AGENT_TIMEOUT`: subprocess timeout for `agent-cli`.
- `SUBNET_CLAIMS_VALIDATOR_AGENT_MODEL`: rigor model name.
- `SUBNET_CLAIMS_VALIDATOR_AGENT_SKILL_DIR`: rigor skill directory.
- `SUBNET_CLAIMS_VALIDATOR_AGENT_CLI_COMMAND`: external runtime wrapper for `agent-cli`.

If a rigor backend fails, the validator emits a controlled critical finding with
`metadata.code = "rigor_agent_failed"` and still writes
`agent_v1_validation_report.json`.

## Skill Use

The default rigor skill is a Claims-specific adaptation of ARA Seal Level 2:

```text
validator/agent_v1/skills/rigor_reviewer
```

It is intentionally narrower than upstream ARA `rigor-reviewer`: it emits
structured findings only, while deterministic validator code computes the score.

