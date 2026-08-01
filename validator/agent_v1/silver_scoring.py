from __future__ import annotations

from .comparison_models import ComparisonCandidate, SilverRecord, SilverScoreBreakdown, SilverUnit
from .models import AgentV1ValidationFinding
from .scoring import PENALTIES, score_findings


IMPORTANCE_WEIGHTS = {
    "central": 0.70,
    "supporting": 0.30,
    "minor": 0.10,
}


def score_miner_against_silver(
    *,
    miner_id: str,
    miner_candidates: list[ComparisonCandidate],
    silver_record: SilverRecord,
    normal_findings: list[AgentV1ValidationFinding] | None = None,
) -> SilverScoreBreakdown:
    normal_findings = normal_findings or []
    miner_candidate_ids = {candidate.candidate_id for candidate in miner_candidates}
    findings: list[AgentV1ValidationFinding] = []
    covered: list[str] = []
    missing: list[str] = []
    improvements: list[str] = []
    required_units = [unit for unit in silver_record.silver_units if unit.required_for_completeness]
    if not required_units and not silver_record.invalid_miner_candidates:
        findings.append(
            AgentV1ValidationFinding(
                finding_id="SV001",
                pass_name="silver_comparison",
                dimension="completeness",
                severity="blocker",
                target_type="silver_record",
                target_id=silver_record.silver_record_id,
                message="Silver scoring record has no required units or invalid miner candidates.",
                metadata={"code": "empty_silver_record"},
            )
        )

    for unit in silver_record.silver_units:
        is_covered = bool(miner_candidate_ids.intersection(unit.equivalent_candidate_ids))
        if unit.required_for_completeness:
            if is_covered:
                covered.append(unit.silver_unit_id)
            else:
                missing.append(unit.silver_unit_id)
                findings.append(_missing_finding(unit, len(findings) + 1))
        elif is_covered:
            improvements.append(unit.silver_unit_id)

    invalid_extras = [candidate for candidate in silver_record.invalid_miner_candidates if candidate.miner_id == miner_id]
    for invalid in invalid_extras:
        findings.append(
            AgentV1ValidationFinding(
                finding_id=f"SV{len(findings) + 1:03d}",
                pass_name="silver_comparison",
                dimension="claim_accuracy",
                severity=invalid.severity,
                target_type="miner_claim",
                target_id=invalid.candidate_id,
                message="Miner submitted a claim rejected by Silver adjudication.",
                evidence_span=invalid.evidence_span,
                metadata={"code": "invalid_extra_candidate", "adjudication_case_id": invalid.adjudication_case_id},
            )
        )

    all_findings = [*normal_findings, *findings]
    coverage = _coverage(silver_record.silver_units, covered)
    quality = _quality(all_findings)
    _finding_score, _finding_passed, summary = score_findings(all_findings)
    score = _silver_score(
        coverage=coverage,
        quality=quality,
        empty_silver_record=any(finding.metadata.get("code") == "empty_silver_record" for finding in findings),
    )
    return SilverScoreBreakdown(
        paper_id=silver_record.paper_id,
        miner_id=miner_id,
        silver_record_id=silver_record.silver_record_id,
        coverage=coverage,
        quality=quality,
        score=score,
        covered_required_silver_units=covered,
        missing_required_silver_units=missing,
        accepted_improvements=improvements,
        invalid_extra_candidates=[candidate.candidate_id for candidate in invalid_extras],
        findings=findings,
        metadata={
            "normal_finding_count": len(normal_findings),
            "passed": score > 0,
            "finding_summary": summary,
            "formula": "score = coverage * quality; empty Silver records score 0",
        },
    )


def _missing_finding(unit: SilverUnit, index: int) -> AgentV1ValidationFinding:
    severity = "critical" if unit.importance == "central" else "major" if unit.importance == "supporting" else "minor"
    return AgentV1ValidationFinding(
        finding_id=f"SV{index:03d}",
        pass_name="silver_comparison",
        dimension="completeness",
        severity=severity,
        target_type="silver_unit",
        target_id=unit.silver_unit_id,
        message=f"Miner submission has no aligned equivalent for a {unit.importance} Silver record.",
        metadata={"code": "missing_silver_record", "importance": unit.importance},
    )


def _coverage(units: list[SilverUnit], covered_unit_ids: list[str]) -> float:
    required_units = [unit for unit in units if unit.required_for_completeness]
    denominator = sum(IMPORTANCE_WEIGHTS[unit.importance] for unit in required_units)
    if denominator <= 0:
        return 1.0
    covered = set(covered_unit_ids)
    numerator = sum(IMPORTANCE_WEIGHTS[unit.importance] for unit in required_units if unit.silver_unit_id in covered)
    return round(numerator / denominator, 4)


def _quality(findings: list[AgentV1ValidationFinding]) -> float:
    quality_penalty = sum(PENALTIES.get(finding.severity, 0.0) for finding in findings if finding.dimension != "completeness")
    return max(0.0, round(1.0 - quality_penalty, 4))


def _silver_score(*, coverage: float, quality: float, empty_silver_record: bool) -> float:
    if empty_silver_record:
        return 0.0
    return max(0.0, round(coverage * quality, 4))
