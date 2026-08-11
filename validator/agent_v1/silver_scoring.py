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
    scored_units = _scored_units(silver_record.silver_units)
    if not scored_units and not silver_record.invalid_miner_candidates:
        findings.append(
            AgentV1ValidationFinding(
                finding_id="SV001",
                pass_name="silver_comparison",
                dimension="completeness",
                severity="blocker",
                target_type="silver_record",
                target_id=silver_record.silver_record_id,
                message="Silver scoring record has no scored units or invalid miner candidates.",
                metadata={"code": "empty_silver_record"},
            )
        )

    for unit in silver_record.silver_units:
        is_covered = bool(miner_candidate_ids.intersection(unit.equivalent_candidate_ids))
        if unit in scored_units:
            if is_covered:
                covered.append(unit.silver_unit_id)
                if unit.scoring_mode == "accepted_improvement":
                    improvements.append(unit.silver_unit_id)
            else:
                missing.append(unit.silver_unit_id)
                findings.append(_missing_finding(unit, len(findings) + 1))
        elif is_covered and unit.scoring_mode == "accepted_improvement":
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

    coverage = _coverage(scored_units, covered)
    diagnostic_quality, _diagnostic_passed, diagnostic_summary = score_findings(normal_findings)
    adjudication_quality = _adjudication_quality(findings)
    quality = max(0.0, round(diagnostic_quality * adjudication_quality, 4))
    all_findings = [*normal_findings, *findings]
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
        findings=all_findings,
        metadata={
            "normal_finding_count": len(normal_findings),
            "silver_finding_count": len(findings),
            "passed": score > 0,
            "finding_summary": summary,
            "diagnostic_finding_summary": diagnostic_summary,
            "coverage": coverage,
            "diagnostic_quality": diagnostic_quality,
            "adjudication_quality": adjudication_quality,
            "quality_formula": "diagnostic_quality * adjudication_quality",
            "covered_silver_units": covered,
            "missing_silver_units": missing,
            "formula": "score = Silver coverage * diagnostic quality * adjudication quality; every scored Silver unit, including accepted improvements, contributes to coverage",
        },
    )


def _missing_finding(unit: SilverUnit, index: int) -> AgentV1ValidationFinding:
    severity = "critical" if unit.importance == "central" else "major" if unit.importance == "supporting" else "minor"
    unit_kind = "accepted improvement" if unit.scoring_mode == "accepted_improvement" else unit.importance
    return AgentV1ValidationFinding(
        finding_id=f"SV{index:03d}",
        pass_name="silver_comparison",
        dimension="completeness",
        severity=severity,
        target_type="silver_unit",
        target_id=unit.silver_unit_id,
        message=f"Miner submission has no aligned equivalent for a {unit_kind} Silver record.",
        metadata={"code": "missing_silver_record", "importance": unit.importance, "scoring_mode": unit.scoring_mode},
    )


def _scored_units(units: list[SilverUnit]) -> list[SilverUnit]:
    return [unit for unit in units if unit.required_for_completeness or unit.scoring_mode == "accepted_improvement"]


def _coverage(scored_units: list[SilverUnit], covered_unit_ids: list[str]) -> float:
    denominator = sum(IMPORTANCE_WEIGHTS[unit.importance] for unit in scored_units)
    if denominator <= 0:
        return 1.0
    covered = set(covered_unit_ids)
    numerator = sum(IMPORTANCE_WEIGHTS[unit.importance] for unit in scored_units if unit.silver_unit_id in covered)
    return round(numerator / denominator, 4)


def _adjudication_quality(findings: list[AgentV1ValidationFinding]) -> float:
    quality_penalty = sum(PENALTIES.get(finding.severity, 0.0) for finding in findings if finding.dimension != "completeness")
    return max(0.0, round(1.0 - quality_penalty, 4))


def _silver_score(*, coverage: float, quality: float, empty_silver_record: bool) -> float:
    if empty_silver_record:
        return 0.0
    return max(0.0, round(coverage * quality, 4))
