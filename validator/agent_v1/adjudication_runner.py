from __future__ import annotations

import hashlib
import inspect
import json
import logging
import os
import time
from concurrent.futures import Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from typing import Any, Callable, Protocol

from .adjudication_consensus import aggregate_adjudication_votes
from .adjudication_models import AdjudicationConsensus, AdjudicationContextBundle, AdjudicationVote


LOGGER = logging.getLogger(__name__)


class AdjudicationPass(Protocol):
    pass_id: str
    adjudication_profile_id: str
    model_runtime_id: str

    def run(self, context: AdjudicationContextBundle) -> AdjudicationVote:
        ...


@dataclass(frozen=True)
class _BatchJob:
    pass_index: int
    adjudication_pass: AdjudicationPass
    contexts: list[AdjudicationContextBundle]
    attempt: int = 0
    split_depth: int = 0
    fallback: bool = False


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
    progress_sink: Callable[[list[AdjudicationContextBundle], list[AdjudicationVote]], None] | None = None,
) -> list[AdjudicationConsensus]:
    if not contexts:
        return []
    batch_size = max(1, int(batch_size or 1))
    direct_votes = _run_pass_batches(
        contexts,
        passes=passes,
        batch_size=batch_size,
        max_workers=max_workers,
        progress_sink=progress_sink,
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
            progress_sink=progress_sink,
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
    progress_sink: Callable[[list[AdjudicationContextBundle], list[AdjudicationVote]], None] | None = None,
) -> dict[str, list[AdjudicationVote]]:
    input_token_limit = _env_int("CLAIMS_SILVER_ADJUDICATION_BATCH_INPUT_TOKENS", 120_000)
    batch_retries = max(0, _env_int("CLAIMS_SILVER_ADJUDICATION_BATCH_RETRIES", 1))
    fallback_limit = max(0, _env_int("CLAIMS_SILVER_ADJUDICATION_FALLBACK_MAX_CALLS", 256))
    wall_timeout = max(0.0, _env_float("CLAIMS_SILVER_ADJUDICATION_WALL_TIMEOUT", 1800.0))
    deadline = time.monotonic() + wall_timeout if wall_timeout else None

    packed_batches = _pack_context_batches(
        contexts,
        max_items=batch_size,
        input_token_limit=input_token_limit,
    )
    pending = [
        _BatchJob(pass_index, adjudication_pass, batch)
        for pass_index, adjudication_pass in enumerate(passes)
        for batch in packed_batches
    ]
    completed: dict[tuple[int, str], AdjudicationVote] = {}
    fallback_calls = 0
    worker_count = max(1, int(max_workers or 1))

    LOGGER.info(
        "Silver adjudication schedule cases=%d passes=%d batches=%d workers=%d "
        "batch_size=%d input_token_limit=%d wall_timeout=%.1fs",
        len(contexts),
        len(passes),
        len(pending),
        worker_count,
        batch_size,
        input_token_limit,
        wall_timeout,
    )

    while pending:
        LOGGER.info(
            "Silver adjudication wave pending_batches=%d fallback_calls=%d fallback_limit=%d",
            len(pending),
            fallback_calls,
            fallback_limit,
        )
        if deadline is not None and time.monotonic() >= deadline:
            _record_failed_jobs(completed, pending, reason="adjudication wall-clock deadline exceeded")
            break

        runnable: list[_BatchJob] = []
        deferred: list[_BatchJob] = []
        for job in pending:
            if job.fallback and fallback_limit and fallback_calls >= fallback_limit:
                deferred.append(job)
                continue
            runnable.append(job)
            if job.fallback:
                fallback_calls += 1
        if deferred:
            _record_failed_jobs(completed, deferred, reason="adjudication fallback call budget exhausted")
        if not runnable:
            break

        executor = ThreadPoolExecutor(
            max_workers=min(worker_count, len(runnable)),
            thread_name_prefix="claims-adjudication",
        )
        request_timeout = None if deadline is None else max(0.001, deadline - time.monotonic())
        futures: dict[Future[list[AdjudicationVote]], _BatchJob] = {
            executor.submit(_run_batch_job, job, request_timeout): job for job in runnable
        }
        timeout = None if deadline is None else max(0.0, deadline - time.monotonic())
        done, not_done = wait(futures, timeout=timeout)
        next_pending: list[_BatchJob] = []
        for future in done:
            job = futures[future]
            prior_keys = set(completed)
            try:
                votes = future.result()
            except Exception as exc:  # Defensive: pass implementations normally return failure votes.
                LOGGER.warning(
                    "Silver adjudication batch raised pass=%s cases=%d attempt=%d depth=%d error=%s",
                    job.adjudication_pass.pass_id,
                    len(job.contexts),
                    job.attempt,
                    job.split_depth,
                    exc,
                )
                votes = [_failed_vote(context, job.adjudication_pass, f"{type(exc).__name__}: {exc}") for context in job.contexts]
            next_pending.extend(
                _consume_batch_result(
                    completed,
                    job,
                    votes,
                    batch_retries=batch_retries,
                )
            )
            if progress_sink is not None:
                persisted_contexts = [
                    context
                    for context in job.contexts
                    if (job.pass_index, context.case.case_id) in completed
                    and (job.pass_index, context.case.case_id) not in prior_keys
                ]
                persisted_votes = [
                    completed[(job.pass_index, context.case.case_id)]
                    for context in persisted_contexts
                ]
                if persisted_votes:
                    progress_sink(persisted_contexts, persisted_votes)
        for future in not_done:
            future.cancel()
            job = futures[future]
            _record_failed_jobs(completed, [job], reason="adjudication wall-clock deadline exceeded")
        executor.shutdown(wait=not not_done, cancel_futures=True)
        if not_done:
            break
        pending = next_pending

    votes_by_case: dict[str, list[AdjudicationVote]] = {}
    for context in contexts:
        case_id = context.case.case_id
        votes_by_case[case_id] = [
            completed[(pass_index, case_id)]
            for pass_index in range(len(passes))
            if (pass_index, case_id) in completed
        ]
    return votes_by_case


def _run_batch_job(job: _BatchJob, timeout_seconds: float | None) -> list[AdjudicationVote]:
    started = time.perf_counter()
    operation_id = _batch_operation_id(job)
    LOGGER.info(
        "Silver adjudication batch start operation=%s pass=%s cases=%d attempt=%d depth=%d fallback=%s timeout=%s",
        operation_id,
        job.adjudication_pass.pass_id,
        len(job.contexts),
        job.attempt,
        job.split_depth,
        job.fallback,
        f"{timeout_seconds:.1f}s" if timeout_seconds is not None else "none",
    )
    run_many = getattr(job.adjudication_pass, "run_many", None)
    if callable(run_many) and len(job.contexts) > 1:
        votes = list(_invoke_with_timeout(run_many, job.contexts, timeout_seconds=timeout_seconds))
    else:
        votes = [
            _invoke_with_timeout(job.adjudication_pass.run, context, timeout_seconds=timeout_seconds)
            for context in job.contexts
        ]
    failures = sum(1 for vote in votes if _is_operational_failure(vote))
    LOGGER.info(
        "Silver adjudication batch finish operation=%s pass=%s cases=%d failures=%d attempt=%d depth=%d elapsed=%.3fs",
        operation_id,
        job.adjudication_pass.pass_id,
        len(job.contexts),
        failures,
        job.attempt,
        job.split_depth,
        time.perf_counter() - started,
    )
    return votes


def _batch_operation_id(job: _BatchJob) -> str:
    case_ids = "|".join(context.case.case_id for context in job.contexts)
    digest = hashlib.sha256(case_ids.encode("utf-8")).hexdigest()[:12]
    return f"{job.adjudication_pass.pass_id}:{digest}:{job.attempt}:{job.split_depth}"


def _invoke_with_timeout(callable_obj, argument, *, timeout_seconds: float | None):
    try:
        parameters = inspect.signature(callable_obj).parameters
    except (TypeError, ValueError):
        parameters = {}
    if "timeout_seconds" in parameters:
        return callable_obj(argument, timeout_seconds=timeout_seconds)
    return callable_obj(argument)


def _consume_batch_result(
    completed: dict[tuple[int, str], AdjudicationVote],
    job: _BatchJob,
    votes: list[AdjudicationVote],
    *,
    batch_retries: int,
) -> list[_BatchJob]:
    votes_by_case_id = {vote.case_id: vote for vote in votes}
    failed_contexts: list[AdjudicationContextBundle] = []
    for context in job.contexts:
        case_id = context.case.case_id
        vote = votes_by_case_id.get(case_id)
        if vote is None and len(job.contexts) == 1 and len(votes) == 1:
            vote = votes[0]
        if vote is None or _is_operational_failure(vote):
            failed_contexts.append(context)
            continue
        if vote.case_id != case_id:
            vote = vote.model_copy(update={"case_id": case_id})
        completed[(job.pass_index, case_id)] = vote

    if not failed_contexts:
        return []
    if job.attempt < batch_retries:
        LOGGER.warning(
            "Retrying Silver adjudication batch pass=%s unresolved=%d/%d attempt=%d",
            job.adjudication_pass.pass_id,
            len(failed_contexts),
            len(job.contexts),
            job.attempt + 1,
        )
        return [
            _BatchJob(
                job.pass_index,
                job.adjudication_pass,
                failed_contexts,
                attempt=job.attempt + 1,
                split_depth=job.split_depth,
                fallback=True,
            )
        ]
    if len(failed_contexts) > 1:
        midpoint = max(1, len(failed_contexts) // 2)
        LOGGER.warning(
            "Splitting failed Silver adjudication batch pass=%s unresolved=%d depth=%d",
            job.adjudication_pass.pass_id,
            len(failed_contexts),
            job.split_depth + 1,
        )
        return [
            _BatchJob(
                job.pass_index,
                job.adjudication_pass,
                split,
                attempt=0,
                split_depth=job.split_depth + 1,
                fallback=True,
            )
            for split in (failed_contexts[:midpoint], failed_contexts[midpoint:])
            if split
        ]

    context = failed_contexts[0]
    failure = votes_by_case_id.get(context.case.case_id)
    completed[(job.pass_index, context.case.case_id)] = failure or _failed_vote(
        context,
        job.adjudication_pass,
        "adjudication batch did not return this case",
    )
    return []


def _pack_context_batches(
    contexts: list[AdjudicationContextBundle],
    *,
    max_items: int,
    input_token_limit: int,
) -> list[list[AdjudicationContextBundle]]:
    max_items = max(1, int(max_items or 1))
    if input_token_limit <= 0:
        return [contexts[start : start + max_items] for start in range(0, len(contexts), max_items)]
    batches: list[list[AdjudicationContextBundle]] = []
    current: list[AdjudicationContextBundle] = []
    current_tokens = 0
    for context in contexts:
        estimated_tokens = _estimate_context_tokens(context)
        if current and (len(current) >= max_items or current_tokens + estimated_tokens > input_token_limit):
            batches.append(current)
            current = []
            current_tokens = 0
        current.append(context)
        current_tokens += estimated_tokens
        if len(current) >= max_items:
            batches.append(current)
            current = []
            current_tokens = 0
    if current:
        batches.append(current)
    return batches


def _estimate_context_tokens(context: AdjudicationContextBundle) -> int:
    payload: dict[str, Any] = {
        "case": context.case.model_dump(mode="json"),
        "candidates": [candidate.model_dump(mode="json") for candidate in context.candidates],
        "source_context": context.source_context,
    }
    # A conservative tokenizer-independent estimate. Scientific identifiers and
    # JSON punctuation tokenize less efficiently than ordinary prose.
    return max(1, (len(json.dumps(payload, ensure_ascii=False)) + 2) // 3)


def _record_failed_jobs(
    completed: dict[tuple[int, str], AdjudicationVote],
    jobs: list[_BatchJob],
    *,
    reason: str,
) -> None:
    for job in jobs:
        for context in job.contexts:
            key = (job.pass_index, context.case.case_id)
            completed.setdefault(key, _failed_vote(context, job.adjudication_pass, reason))


def _failed_vote(
    context: AdjudicationContextBundle,
    adjudication_pass: AdjudicationPass,
    reason: str,
) -> AdjudicationVote:
    return AdjudicationVote(
        case_id=context.case.case_id,
        pass_id=adjudication_pass.pass_id,
        adjudication_profile_id=adjudication_pass.adjudication_profile_id,
        model_runtime_id=adjudication_pass.model_runtime_id,
        candidate_order=[candidate.candidate_id for candidate in context.candidates],
        disposition="insufficient_information",
        material_findings=["adjudication_batch_failed"],
        cited_span_ids=[],
        confidence=0.0,
        rationale=reason,
        insufficient_information=True,
    )


def _is_operational_failure(vote: AdjudicationVote) -> bool:
    return any(
        finding in {"adjudication_batch_failed", "adjudication_pass_failed"}
        for finding in vote.material_findings
    )


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default
