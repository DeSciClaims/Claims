from __future__ import annotations

from collections import Counter

from .models import AgentV1ValidationFinding


PENALTIES = {
    "blocker": 1.0,
    "critical": 0.25,
    "major": 0.10,
    "minor": 0.03,
    "warning": 0.01,
    "suggestion": 0.0,
}


def score_findings(findings: list[AgentV1ValidationFinding], *, threshold: float = 0.7) -> tuple[float, bool, dict[str, int]]:
    score = 1.0
    summary = Counter(f.severity for f in findings)
    for finding in findings:
        score -= PENALTIES.get(finding.severity, 0.0)
    score = max(0.0, round(score, 4))
    score = min(score, _score_floor(findings))
    passed = score >= threshold and not any(f.severity in {"blocker", "critical"} for f in findings)
    return score, passed, {key: summary.get(key, 0) for key in PENALTIES}


def _score_floor(findings: list[AgentV1ValidationFinding]) -> float:
    floor = 1.0
    for finding in findings:
        code = str(finding.metadata.get("code") or "")
        if finding.severity == "blocker":
            floor = min(floor, 0.05)
        if code in {"invalid_json", "schema_validation_failed"}:
            floor = min(floor, 0.2)
        if code in {"missing_layer"}:
            floor = min(floor, 0.25)
        if code in {"no_evidence_records"}:
            floor = min(floor, 0.4)
        if code in {"no_source_refs", "quote_not_in_source", "missing_source_span"}:
            floor = min(floor, 0.5)
        if code == "rigor_agent_skipped":
            floor = min(floor, 0.6)
        if code == "rigor_agent_failed":
            floor = min(floor, 0.3)
    return floor
