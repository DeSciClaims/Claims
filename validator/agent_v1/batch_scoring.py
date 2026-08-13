from __future__ import annotations

from collections import defaultdict
from statistics import mean

from pydantic import BaseModel, Field

from .comparison_models import SilverScoreBreakdown


class MinerBatchScore(BaseModel):
    miner_id: str
    paper_count: int
    mean_score: float
    rank: int | None = None
    paper_scores: list[SilverScoreBreakdown] = Field(default_factory=list)


class BatchScoreResult(BaseModel):
    batch_id: str
    miners: list[MinerBatchScore] = Field(default_factory=list)
    winner_miner_id: str | None = None


def score_batch(
    *,
    batch_id: str,
    paper_scores: list[SilverScoreBreakdown],
) -> BatchScoreResult:
    grouped: dict[str, list[SilverScoreBreakdown]] = defaultdict(list)
    for score in paper_scores:
        grouped[score.miner_id].append(score)

    miners = [
        MinerBatchScore(
            miner_id=miner_id,
            paper_count=len(scores),
            mean_score=round(mean(score.score for score in scores), 4),
            paper_scores=scores,
        )
        for miner_id, scores in grouped.items()
    ]
    miners.sort(key=lambda item: (-item.mean_score, item.miner_id))
    rank = 1
    for index, item in enumerate(miners):
        if index > 0 and item.mean_score != miners[index - 1].mean_score:
            rank = index + 1
        item.rank = rank

    winner = miners[0].miner_id if miners and miners[0].mean_score > 0.0 else None
    return BatchScoreResult(batch_id=batch_id, miners=miners, winner_miner_id=winner)
