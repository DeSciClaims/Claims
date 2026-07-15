Return strict JSON with this shape:

```json
{
  "findings": [
    {
      "dimension": "evidence_relevance|falsifiability_quality|scope_calibration|argument_coherence|exploration_integrity|methodological_rigor",
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

Do not include a final score. Deterministic validator code computes the score
from all findings.
