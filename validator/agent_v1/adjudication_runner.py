from __future__ import annotations

from typing import Protocol

from .adjudication_consensus import aggregate_adjudication_votes
from .adjudication_models import AdjudicationConsensus, AdjudicationContextBundle, AdjudicationVote


class AdjudicationPass(Protocol):
    pass_id: str
    adjudication_profile_id: str
    model_runtime_id: str

    def run(self, context: AdjudicationContextBundle) -> AdjudicationVote:
        ...


def run_adjudication_case(
    context: AdjudicationContextBundle,
    *,
    passes: list[AdjudicationPass],
    tiebreak_pass: AdjudicationPass | None = None,
    direct_judge_confidence: float = 0.9,
) -> AdjudicationConsensus:
    votes = [adjudication_pass.run(context) for adjudication_pass in passes]
    consensus = aggregate_adjudication_votes(
        context.case.case_id,
        votes,
        direct_judge_confidence=direct_judge_confidence,
        route="direct",
    )
    if consensus.route != "unresolved" or tiebreak_pass is None:
        return consensus

    tiebreak_votes = [*votes, tiebreak_pass.run(context)]
    tiebreak = aggregate_adjudication_votes(
        context.case.case_id,
        tiebreak_votes,
        direct_judge_confidence=direct_judge_confidence,
        route="tiebreak",
    )
    if tiebreak.route == "unresolved":
        tiebreak.route = "manual_review"
    return tiebreak
