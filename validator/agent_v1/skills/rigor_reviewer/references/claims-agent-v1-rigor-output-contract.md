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

Emit direct `grounding_adjudication` findings when the cited source spans do not
semantically support the artifact text. This includes unsupported quotes,
unsupported load-bearing numbers, missing multi-span support, scope drift, and
citations that point to the wrong source span. Deterministic grounding findings
are contract findings and normally cover only missing payloads, missing source
refs, missing span IDs, or invalid source roles.

Do not include a final score. Deterministic validator code computes the score
from all findings.

For older validation runs only, you may suppress a deterministic
`quote_not_in_source` or `number_not_grounded` finding when the cited source
span is present and semantically supports the artifact text despite PDF
formatting, paraphrase, notation, or line-break differences. Do not suppress
missing source payload, missing source ref, missing span ID, or invalid role
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
