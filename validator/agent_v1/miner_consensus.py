from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, Field

from .adjudication_models import AdjudicationConsensus, AdjudicationDisposition, AdjudicationVote


class MinerConsensusVote(BaseModel):
    consensus_case_id: str
    case_id: str
    uid: int
    hotkey: str = ""
    disposition: AdjudicationDisposition
    confidence: float = Field(default=1.0, ge=0, le=1)
    rationale: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


@dataclass(frozen=True)
class MinerConsensusRule:
    min_votes: int = 3
    agreement_threshold: float = 2 / 3
    max_weight_per_hotkey: float = 0.30


def aggregate_miner_consensus_votes(
    *,
    case_id: str,
    votes: list[MinerConsensusVote],
    excluded_uids: set[int] | None = None,
    rule: MinerConsensusRule | None = None,
) -> AdjudicationConsensus:
    rule = rule or MinerConsensusRule()
    excluded_uids = excluded_uids or set()
    eligible_votes = [vote for vote in votes if vote.uid not in excluded_uids]
    if len(eligible_votes) < rule.min_votes:
        return AdjudicationConsensus(
            case_id=case_id,
            route="miner_consensus",
            agreement_rate=0.0,
            rationale=f"Miner consensus unresolved: {len(eligible_votes)} eligible votes below min_votes={rule.min_votes}.",
        )

    weights = _vote_weights(eligible_votes, max_weight_per_hotkey=rule.max_weight_per_hotkey)
    totals: Counter[str] = Counter()
    confidence_by_disposition: dict[str, list[float]] = {}
    for vote in eligible_votes:
        weight = weights[(vote.uid, vote.hotkey)]
        totals[vote.disposition] += weight
        confidence_by_disposition.setdefault(vote.disposition, []).append(vote.confidence)

    disposition, winning_weight = totals.most_common(1)[0]
    total_weight = sum(totals.values())
    agreement_rate = winning_weight / total_weight if total_weight else 0.0
    converted_votes = [
        AdjudicationVote(
            case_id=case_id,
            pass_id=f"miner_uid_{vote.uid}",
            adjudication_profile_id="v1_miner_consensus",
            model_runtime_id="miner_vote",
            disposition=vote.disposition,
            material_findings=[vote.disposition],
            confidence=vote.confidence,
            rationale=vote.rationale,
        )
        for vote in eligible_votes
    ]
    if agreement_rate >= rule.agreement_threshold:
        return AdjudicationConsensus(
            case_id=case_id,
            votes=converted_votes,
            final_disposition=disposition,  # type: ignore[arg-type]
            final_confidence=round(min(confidence_by_disposition.get(disposition, [0.0])), 4),
            agreement_rate=round(agreement_rate, 4),
            route="miner_consensus",
            rationale="Miner consensus resolved the adjudication case.",
        )
    return AdjudicationConsensus(
        case_id=case_id,
        votes=converted_votes,
        agreement_rate=round(agreement_rate, 4),
        route="miner_consensus",
        rationale="Miner consensus did not reach the agreement threshold.",
    )


def miner_consensus_outcome_payload(
    *,
    outcome_id: str,
    network: str,
    consensus_case_id: str,
    run_id: str,
    batch_id: str,
    paper_id: str,
    consensus: AdjudicationConsensus,
) -> dict[str, Any]:
    return {
        "outcome_id": outcome_id,
        "network": network,
        "consensus_case_id": consensus_case_id,
        "case_id": consensus.case_id,
        "run_id": run_id,
        "batch_id": batch_id,
        "paper_id": paper_id,
        "final_disposition": consensus.final_disposition,
        "agreement_rate": consensus.agreement_rate,
        "confidence": consensus.final_confidence,
        "status": "resolved" if consensus.final_disposition else "unresolved",
        "votes": [vote.model_dump(mode="json") for vote in consensus.votes],
    }


def _vote_weights(votes: list[MinerConsensusVote], *, max_weight_per_hotkey: float) -> dict[tuple[int, str], float]:
    raw = {(vote.uid, vote.hotkey): 1.0 for vote in votes}
    hotkey_counts: Counter[str] = Counter(vote.hotkey or f"uid:{vote.uid}" for vote in votes)
    capped: dict[tuple[int, str], float] = {}
    for vote in votes:
        hotkey = vote.hotkey or f"uid:{vote.uid}"
        correlated_share = 1.0 / hotkey_counts[hotkey]
        capped[(vote.uid, vote.hotkey)] = min(correlated_share, max_weight_per_hotkey)
    return capped or raw
