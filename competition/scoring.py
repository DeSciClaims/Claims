"""Reuse the canonical Silver scorer in a fixed challenger/incumbent pair."""

from __future__ import annotations

from dataclasses import dataclass

from validator.agent_v1.comparison_models import ComparisonCandidate, SilverRecord, SilverScoreBreakdown
from validator.agent_v1.models import AgentV1ValidationFinding
from validator.agent_v1.silver_scoring import score_miner_against_silver

from .policy import PaperPairScore


@dataclass(frozen=True)
class PairwisePaperResult:
    challenger: SilverScoreBreakdown
    incumbent: SilverScoreBreakdown
    pair: PaperPairScore


def score_pairwise_paper(
    *, challenger_id: str, incumbent_id: str, silver_record: SilverRecord,
    challenger_candidates: list[ComparisonCandidate],
    incumbent_candidates: list[ComparisonCandidate],
    challenger_findings: list[AgentV1ValidationFinding] | None = None,
    incumbent_findings: list[AgentV1ValidationFinding] | None = None,
    equivalence_shared_coverage: bool = True,
) -> PairwisePaperResult:
    """Score both outputs against the same accepted units and judge pass.

    Other miners may have contributed units to ``silver_record``; their
    outputs never enter the equivalence-credit divisor for this pair.
    """
    if not challenger_id or not incumbent_id or challenger_id == incumbent_id:
        raise ValueError("pairwise scoring requires distinct nonempty IDs")
    participants = {challenger_id, incumbent_id}
    challenger = score_miner_against_silver(
        miner_id=challenger_id, miner_candidates=challenger_candidates,
        silver_record=silver_record, normal_findings=challenger_findings,
        equivalence_shared_coverage=equivalence_shared_coverage,
        credit_miners=participants,
    )
    incumbent = score_miner_against_silver(
        miner_id=incumbent_id, miner_candidates=incumbent_candidates,
        silver_record=silver_record, normal_findings=incumbent_findings,
        equivalence_shared_coverage=equivalence_shared_coverage,
        credit_miners=participants,
    )
    return PairwisePaperResult(
        challenger, incumbent,
        PaperPairScore(silver_record.paper_id, challenger.score, incumbent.score),
    )
