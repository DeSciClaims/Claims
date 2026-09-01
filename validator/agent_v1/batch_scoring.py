from __future__ import annotations

from collections import defaultdict
from statistics import mean
from typing import Any

from pydantic import BaseModel, Field

from .comparison_models import SilverScoreBreakdown


class MinerBatchScore(BaseModel):
    miner_id: str
    paper_count: int
    expected_paper_count: int = 0
    eligible_paper_count: int = 0
    submitted_paper_count: int = 0
    missing_paper_ids: list[str] = Field(default_factory=list)
    validator_failed_paper_ids: list[str] = Field(default_factory=list)
    mean_score: float
    median_score: float = 0.0
    min_score: float = 0.0
    batch_score: float = 0.0
    rank: int | None = None
    winner: bool = False
    payout_weight: float = 0.0
    selection_lane: str | None = None
    newcomer: bool = False
    overall_payout_weight: float = 0.0
    newcomer_bonus_weight: float = 0.0
    paper_scores: list[SilverScoreBreakdown] = Field(default_factory=list)


class BatchScoreResult(BaseModel):
    batch_id: str
    miners: list[MinerBatchScore] = Field(default_factory=list)
    winner_miner_id: str | None = None
    winner_miner_ids: list[str] = Field(default_factory=list)
    expected_paper_ids: list[str] = Field(default_factory=list)
    eligible_paper_ids: list[str] = Field(default_factory=list)
    validator_failed_paper_ids: list[str] = Field(default_factory=list)
    outcome: str = "completed"
    payout_policy: dict[str, Any] = Field(default_factory=dict)


def score_batch(
    *,
    batch_id: str,
    paper_scores: list[SilverScoreBreakdown],
    expected_paper_ids: list[str] | None = None,
    eligible_paper_ids: list[str] | None = None,
    validator_failed_paper_ids: list[str] | None = None,
    payout_mode: str = "winner-takes-most",
    winner_share: float = 0.70,
    runner_up_slots: int = 4,
    runner_up_decay: float = 0.5,
    selection_lanes: dict[str, str] | None = None,
    selection_policy: dict[str, Any] | None = None,
) -> BatchScoreResult:
    expected_papers = list(dict.fromkeys(expected_paper_ids or []))
    eligible_papers = list(
        dict.fromkeys(
            eligible_paper_ids
            or [score.paper_id for score in paper_scores if score.paper_id]
        )
    )
    failed_papers = list(dict.fromkeys(validator_failed_paper_ids or []))
    grouped: dict[str, list[SilverScoreBreakdown]] = defaultdict(list)
    for score in paper_scores:
        grouped[score.miner_id].append(score)

    miners: list[MinerBatchScore] = []
    for miner_id, scores in grouped.items():
        values = [float(score.score) for score in scores]
        missing_papers = [
            score.paper_id
            for score in scores
            if str(score.metadata.get("code") or "") == "missing_paper_submission"
        ]
        sorted_values = sorted(values)
        midpoint = len(sorted_values) // 2
        median_score = (
            sorted_values[midpoint]
            if len(sorted_values) % 2
            else (sorted_values[midpoint - 1] + sorted_values[midpoint]) / 2
        ) if sorted_values else 0.0
        mean_score = round(mean(values), 4) if values else 0.0
        miners.append(
            MinerBatchScore(
                miner_id=miner_id,
                paper_count=len(scores),
                expected_paper_count=len(expected_papers) or len(eligible_papers),
                eligible_paper_count=len(eligible_papers),
                submitted_paper_count=max(0, len(eligible_papers) - len(set(missing_papers))),
                missing_paper_ids=list(dict.fromkeys(missing_papers)),
                validator_failed_paper_ids=failed_papers,
                mean_score=mean_score,
                median_score=round(float(median_score), 4),
                min_score=round(min(values), 4) if values else 0.0,
                batch_score=mean_score,
                selection_lane=(selection_lanes or {}).get(miner_id),
                newcomer=(selection_lanes or {}).get(miner_id) == "qualification",
                paper_scores=scores,
            )
        )
    miners.sort(key=lambda item: (-item.batch_score, item.miner_id))
    rank = 1
    for index, item in enumerate(miners):
        if index > 0 and item.batch_score != miners[index - 1].batch_score:
            rank = index + 1
        item.rank = rank

    policy_details: dict[str, Any] = {}
    if payout_mode == "proportional":
        total = sum(max(item.batch_score, 0.0) for item in miners)
        payout_weights = {
            item.miner_id: max(item.batch_score, 0.0) / total if total > 0.0 else 0.0
            for item in miners
        }
    else:
        payout_weights = winner_takes_most_weights(
            {item.miner_id: item.batch_score for item in miners},
            winner_share=winner_share,
            runner_up_slots=runner_up_slots,
            runner_up_decay=runner_up_decay,
        )
    if payout_mode == "bucket":
        payout_weights, policy_details = bucket_payout_weights(
            miners,
            overall_weights=payout_weights,
            selection_policy=selection_policy or {},
        )
    winner_ids = [
        item.miner_id
        for item in miners
        if item.rank == 1 and item.batch_score > 0.0
    ]
    for item in miners:
        item.winner = item.miner_id in winner_ids
        item.payout_weight = round(float(payout_weights.get(item.miner_id, 0.0)), 8)

    return BatchScoreResult(
        batch_id=batch_id,
        miners=miners,
        winner_miner_id=winner_ids[0] if winner_ids else None,
        winner_miner_ids=winner_ids,
        expected_paper_ids=expected_papers,
        eligible_paper_ids=eligible_papers,
        validator_failed_paper_ids=failed_papers,
        outcome="degraded" if failed_papers else "completed",
        payout_policy={
            "mode": payout_mode.replace("-", "_"),
            "winner_share": winner_share,
            "runner_up_slots": runner_up_slots,
            "runner_up_decay": runner_up_decay,
            **policy_details,
        },
    )


def bucket_payout_weights(
    miners: list[MinerBatchScore],
    *,
    overall_weights: dict[str, float],
    selection_policy: dict[str, Any],
) -> tuple[dict[str, float], dict[str, Any]]:
    configured_share = max(0.0, min(0.40, float(selection_policy.get("newcomer_share") or 0.0)))
    minimum_score = max(0.0, min(1.0, float(selection_policy.get("newcomer_min_score") or 0.0)))
    valid_newcomers = [
        item
        for item in miners
        if item.newcomer
        and item.submitted_paper_count > 0
        and item.batch_score > minimum_score
    ]
    best_newcomer_score = max((item.batch_score for item in valid_newcomers), default=None)
    newcomer_winners = [
        item
        for item in valid_newcomers
        if best_newcomer_score is not None and item.batch_score == best_newcomer_score
    ]
    applied_share = configured_share if newcomer_winners else 0.0
    overall_share = 1.0 - applied_share
    bonus_each = applied_share / len(newcomer_winners) if newcomer_winners else 0.0
    newcomer_ids = {item.miner_id for item in newcomer_winners}
    final: dict[str, float] = {}
    for item in miners:
        item.overall_payout_weight = round(float(overall_weights.get(item.miner_id, 0.0)) * overall_share, 8)
        item.newcomer_bonus_weight = round(bonus_each if item.miner_id in newcomer_ids else 0.0, 8)
        final[item.miner_id] = item.overall_payout_weight + item.newcomer_bonus_weight
    return final, {
        **selection_policy,
        "configured_newcomer_share": configured_share,
        "applied_newcomer_share": applied_share,
        "applied_overall_share": overall_share,
        "newcomer_winner_miner_ids": sorted(newcomer_ids),
        "newcomer_bonus_returned_to_overall": bool(configured_share > 0.0 and not newcomer_winners),
    }


def winner_takes_most_weights(
    scores: dict[str, float],
    *,
    winner_share: float = 0.70,
    runner_up_slots: int = 4,
    runner_up_decay: float = 0.5,
) -> dict[str, float]:
    """Return a rank payout, splitting occupied rank slots across score ties."""

    if not 0.0 <= winner_share <= 1.0:
        raise ValueError("winner_share must be between zero and one")
    if runner_up_slots < 0:
        raise ValueError("runner_up_slots must be non-negative")
    if runner_up_decay <= 0.0:
        raise ValueError("runner_up_decay must be positive")

    result = {miner_id: 0.0 for miner_id in scores}
    ranked = sorted(
        ((miner_id, max(float(score), 0.0)) for miner_id, score in scores.items() if float(score) > 0.0),
        key=lambda item: (-item[1], item[0]),
    )
    if not ranked:
        return result
    if len(ranked) == 1:
        result[ranked[0][0]] = 1.0
        return result

    paid_runner_count = min(runner_up_slots, len(ranked) - 1)
    runner_mass = max(0.0, 1.0 - winner_share)
    runner_factors = [runner_up_decay**index for index in range(paid_runner_count)]
    runner_total = sum(runner_factors)
    slot_weights = [winner_share]
    if runner_total > 0.0:
        slot_weights.extend(runner_mass * factor / runner_total for factor in runner_factors)

    index = 0
    while index < len(ranked):
        end = index + 1
        while end < len(ranked) and ranked[end][1] == ranked[index][1]:
            end += 1
        occupied_mass = sum(slot_weights[index : min(end, len(slot_weights))])
        if occupied_mass > 0.0:
            shared = occupied_mass / (end - index)
            for miner_id, _score in ranked[index:end]:
                result[miner_id] = shared
        index = end

    total = sum(result.values())
    if total > 0.0:
        result = {miner_id: weight / total for miner_id, weight in result.items()}
    return result
