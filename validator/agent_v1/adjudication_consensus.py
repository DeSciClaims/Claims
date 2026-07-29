from __future__ import annotations

from collections import Counter

from .adjudication_models import AdjudicationConsensus, AdjudicationRoute, AdjudicationVote


def aggregate_adjudication_votes(
    case_id: str,
    votes: list[AdjudicationVote],
    *,
    direct_judge_confidence: float = 0.9,
    route: AdjudicationRoute = "direct",
) -> AdjudicationConsensus:
    if not votes:
        return AdjudicationConsensus(case_id=case_id, votes=[], route="unresolved", rationale="No adjudication votes.")

    eligible_votes = [vote for vote in votes if not vote.insufficient_information and vote.disposition != "insufficient_information"]
    if not eligible_votes:
        return AdjudicationConsensus(
            case_id=case_id,
            votes=votes,
            final_disposition=None,
            final_confidence=0.0,
            agreement_rate=0.0,
            route="unresolved",
            rationale="All adjudication passes returned insufficient information.",
        )

    counts = Counter(vote.disposition for vote in eligible_votes)
    disposition, count = counts.most_common(1)[0]
    agreement_rate = count / len(votes)
    agreeing_votes = [vote for vote in eligible_votes if vote.disposition == disposition]
    confidence = min(vote.confidence for vote in agreeing_votes)

    if count == len(votes) and confidence >= direct_judge_confidence:
        return AdjudicationConsensus(
            case_id=case_id,
            votes=votes,
            final_disposition=disposition,
            final_confidence=round(confidence, 4),
            agreement_rate=round(agreement_rate, 4),
            route=route,
            rationale="Required adjudication passes agreed with sufficient confidence.",
        )

    majority_confidence_floor = min(0.75, direct_judge_confidence)
    if count >= 2 and agreement_rate >= (2 / 3) and confidence >= majority_confidence_floor:
        return AdjudicationConsensus(
            case_id=case_id,
            votes=votes,
            final_disposition=disposition,
            final_confidence=round(confidence, 4),
            agreement_rate=round(agreement_rate, 4),
            route=route,
            rationale=f"Adjudication resolved by {count}/{len(votes)} non-abstaining majority.",
        )

    return AdjudicationConsensus(
        case_id=case_id,
        votes=votes,
        final_disposition=None,
        final_confidence=round(confidence, 4),
        agreement_rate=round(agreement_rate, 4),
        route="unresolved",
        rationale="Adjudication passes did not satisfy direct-decision requirements.",
    )
