Return strict JSON with this shape:

```json
{
  "findings": [
    {
      "dimension": "evidence_relevance|falsifiability_quality|scope_calibration|argument_coherence|exploration_integrity|methodological_rigor|grounding_adjudication",
      "severity": "critical|major|minor|warning|suggestion",
      "target_type": "claim|evidence|experiment|trace|logic|src|artifact",
      "target_id": "C01",
      "message": "Short factual finding.",
      "evidence_span": "Exact artifact quote or null for absence.",
      "suggestion": "Actionable repair suggestion.",
      "metadata": {}
    }
  ]
}
```

If the deterministic grounding finding is valid, do not re-emit the same
`quote_not_in_source` or `number_not_grounded` finding as a rigor finding. Leave
that issue in `grounding_findings.json` so the validator counts it once. Emit a
rigor finding only for additional semantic-rigor problems beyond the grounding
defect.

Do not include a final score. Deterministic validator code computes the score
from all findings.

For LLM-adjudicated validation, you may suppress a deterministic grounding
finding only when the cited source span is present and semantically supports
the quoted claim/evidence despite PDF formatting, paraphrase, notation, or
line-break differences. Do not suppress missing source payload or missing span
findings. Emit a zero-penalty suggestion with:

```json
{
  "dimension": "grounding_adjudication",
  "severity": "suggestion",
  "target_type": "claim",
  "target_id": "C01",
  "message": "Deterministic grounding finding G001 is semantically supported by the cited span.",
  "evidence_span": "Short exact excerpt from the cited span that supports the artifact text.",
  "suggestion": null,
  "metadata": {
    "code": "grounding_finding_supported",
    "suppresses_finding_id": "G001",
    "cited_span_ids": ["paper-p001-markdown"]
  }
}
```
