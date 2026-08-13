from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
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
    return run_adjudication_cases(
        [context],
        passes=passes,
        tiebreak_pass=tiebreak_pass,
        direct_judge_confidence=direct_judge_confidence,
        batch_size=1,
        max_workers=max(1, len(passes)),
    )[0]


def run_adjudication_cases(
    contexts: list[AdjudicationContextBundle],
    *,
    passes: list[AdjudicationPass],
    tiebreak_pass: AdjudicationPass | None = None,
    direct_judge_confidence: float = 0.9,
    batch_size: int = 1,
    max_workers: int = 1,
) -> list[AdjudicationConsensus]:
    if not contexts:
        return []
    batch_size = max(1, int(batch_size or 1))
    direct_votes = _run_pass_batches(
        contexts,
        passes=passes,
        batch_size=batch_size,
        max_workers=max_workers,
    )
    consensus_by_case: dict[str, AdjudicationConsensus] = {}
    unresolved_contexts: list[AdjudicationContextBundle] = []
    for context in contexts:
        votes = direct_votes.get(context.case.case_id, [])
        consensus = aggregate_adjudication_votes(
            context.case.case_id,
            votes,
            direct_judge_confidence=direct_judge_confidence,
            route="direct",
        )
        consensus_by_case[context.case.case_id] = consensus
        if consensus.route == "unresolved" and tiebreak_pass is not None:
            unresolved_contexts.append(context)

    if unresolved_contexts and tiebreak_pass is not None:
        tiebreak_votes = _run_pass_batches(
            unresolved_contexts,
            passes=[tiebreak_pass],
            batch_size=batch_size,
            max_workers=max_workers,
        )
        for context in unresolved_contexts:
            case_id = context.case.case_id
            votes = [*direct_votes.get(case_id, []), *tiebreak_votes.get(case_id, [])]
            consensus = aggregate_adjudication_votes(
                case_id,
                votes,
                direct_judge_confidence=direct_judge_confidence,
                route="tiebreak",
            )
            if consensus.route == "unresolved":
                consensus.route = "manual_review"
            consensus_by_case[case_id] = consensus

    return [consensus_by_case[context.case.case_id] for context in contexts]


def _run_pass_batches(
    contexts: list[AdjudicationContextBundle],
    *,
    passes: list[AdjudicationPass],
    batch_size: int,
    max_workers: int,
) -> dict[str, list[AdjudicationVote]]:
    jobs = [
        (pass_index, adjudication_pass, contexts[start : start + batch_size])
        for pass_index, adjudication_pass in enumerate(passes)
        for start in range(0, len(contexts), batch_size)
    ]

    def run_job(adjudication_pass: AdjudicationPass, batch: list[AdjudicationContextBundle]):
        run_many = getattr(adjudication_pass, "run_many", None)
        if callable(run_many) and len(batch) > 1:
            return list(run_many(batch))
        return [adjudication_pass.run(context) for context in batch]

    completed: dict[tuple[int, str], AdjudicationVote] = {}
    worker_count = max(1, min(int(max_workers or 1), len(jobs) or 1))
    if worker_count == 1:
        results = [
            (pass_index, batch, run_job(adjudication_pass, batch))
            for pass_index, adjudication_pass, batch in jobs
        ]
    else:
        results = []
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            futures = {
                executor.submit(run_job, adjudication_pass, batch): (pass_index, batch)
                for pass_index, adjudication_pass, batch in jobs
            }
            for future in as_completed(futures):
                pass_index, batch = futures[future]
                results.append((pass_index, batch, future.result()))

    for pass_index, batch, votes in results:
        for context, vote in zip(batch, votes):
            if vote.case_id != context.case.case_id:
                vote = vote.model_copy(update={"case_id": context.case.case_id})
            completed[(pass_index, context.case.case_id)] = vote

    votes_by_case: dict[str, list[AdjudicationVote]] = {}
    for context in contexts:
        case_id = context.case.case_id
        votes_by_case[case_id] = [
            completed[(pass_index, case_id)]
            for pass_index in range(len(passes))
            if (pass_index, case_id) in completed
        ]
    return votes_by_case
