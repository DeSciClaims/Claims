from __future__ import annotations

import hashlib
import json
import logging
import os
import shlex
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import Future, ThreadPoolExecutor, wait
from contextlib import contextmanager
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Callable

from miner.agent_v1.runtime.usage import empty_usage, usage_from_cli_process, usage_from_dspy_lm

from .comparison_models import CandidatePairEdge, ComparisonCandidate, RelationType
from .model_usage import UsageSink, provider_from_model_or_base


ALLOWED_RELATIONS: set[str] = {
    "semantic_equivalent",
    "compatible_refinement",
    "compatible_split_merge",
    "partial_overlap",
    "contradiction",
    "unrelated",
    "insufficient_context",
}
LOGGER = logging.getLogger(__name__)


def classify_candidate_pair(left: ComparisonCandidate, right: ComparisonCandidate) -> CandidatePairEdge:
    return classify_candidate_pair_heuristic(left, right)


def classify_candidate_pair_heuristic(left: ComparisonCandidate, right: ComparisonCandidate) -> CandidatePairEdge:
    confidence = _similarity(left.normalized_statement, right.normalized_statement)
    relation: RelationType
    if left.normalized_statement == right.normalized_statement:
        relation = "semantic_equivalent"
        confidence = 1.0
    elif _contains_numbers(left.normalized_statement) and _contains_numbers(right.normalized_statement) and _numbers(left.normalized_statement) != _numbers(right.normalized_statement):
        relation = "contradiction"
        confidence = max(confidence, 0.75)
    elif confidence >= 0.86:
        relation = "semantic_equivalent"
    elif confidence >= 0.72:
        relation = "compatible_refinement"
    elif confidence >= 0.55:
        relation = "partial_overlap"
    else:
        relation = "unrelated"
    return CandidatePairEdge(
        edge_id=f"{left.candidate_id}::{right.candidate_id}",
        left_candidate_id=left.candidate_id,
        right_candidate_id=right.candidate_id,
        relation=relation,
        confidence=round(confidence, 4),
        rationale="Deterministic text-similarity relation classifier.",
    )


class DSPyRelationClassifier:
    def __init__(
        self,
        *,
        model: str | None = None,
        api_key: str | None = None,
        api_base: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        timeout_seconds: float | None = None,
        dspy_module: Any | None = None,
        program: Any | None = None,
        batch_program: Any | None = None,
        fallback_to_heuristic: bool = True,
        request_gate: Any | None = None,
        usage_sink: UsageSink | None = None,
        stage_key: str = "silver_comparison",
        stage_label: str = "Comparison graph",
        batch_size: int | None = None,
        max_workers: int | None = None,
        fallback_label: str = "DSPy",
    ) -> None:
        self.model = model or os.getenv("CLAIMS_SILVER_RELATION_MODEL") or os.getenv("OPENROUTER_MODEL", "openrouter/openai/gpt-4o-mini")
        self.api_key = api_key if api_key is not None else os.getenv("OPENROUTER_API_KEY", "")
        self.api_base = api_base or os.getenv("CLAIMS_SILVER_RELATION_API_BASE") or os.getenv("OPENROUTER_API_BASE", "https://openrouter.ai/api/v1")
        self.temperature = temperature if temperature is not None else float(os.getenv("CLAIMS_SILVER_RELATION_TEMPERATURE", "1.0"))
        self.max_tokens = max_tokens if max_tokens is not None else int(os.getenv("CLAIMS_SILVER_RELATION_MAX_TOKENS", "2048"))
        self.timeout_seconds = (
            float(timeout_seconds)
            if timeout_seconds is not None
            else float(os.getenv("CLAIMS_SILVER_RELATION_TIMEOUT", "120"))
        )
        self._dspy_module = dspy_module
        self._program = program
        self._base_program = None
        self._batch_program = batch_program
        self._provided_batch_program = batch_program
        self._program_lock = threading.Lock()
        self.fallback_to_heuristic = fallback_to_heuristic
        self.request_gate = request_gate
        self.usage_sink = usage_sink
        self.stage_key = stage_key
        self.stage_label = stage_label
        self.fallback_label = fallback_label
        self.batch_size = max(
            1,
            batch_size if batch_size is not None else _env_int("CLAIMS_SILVER_RELATION_BATCH_SIZE", 16),
        )
        self.max_workers = max(
            1,
            max_workers if max_workers is not None else _env_int("CLAIMS_SILVER_RELATION_MAX_WORKERS", 4),
        )

    @classmethod
    def from_env(cls) -> "DSPyRelationClassifier":
        return cls()

    def __call__(self, left: ComparisonCandidate, right: ComparisonCandidate) -> CandidatePairEdge:
        try:
            payload = self._classify_with_dspy(left, right)
            relation = _coerce_relation(payload.get("relation"))
            confidence = _clamp_float(payload.get("confidence"), default=0.0)
            return CandidatePairEdge(
                edge_id=f"{left.candidate_id}::{right.candidate_id}",
                left_candidate_id=left.candidate_id,
                right_candidate_id=right.candidate_id,
                relation=relation,
                confidence=confidence,
                rationale=str(payload.get("rationale") or ""),
            )
        except Exception as exc:
            if not self.fallback_to_heuristic:
                raise
            edge = classify_candidate_pair_heuristic(left, right)
            edge.rationale = f"{edge.rationale} {self.fallback_label} classifier fallback: {type(exc).__name__}: {exc}"
            return edge

    def classify_many(
        self,
        pairs: list[tuple[ComparisonCandidate, ComparisonCandidate]],
    ) -> list[CandidatePairEdge]:
        if not pairs:
            return []
        input_token_limit = max(0, _env_int("CLAIMS_SILVER_RELATION_BATCH_INPUT_TOKENS", 100_000))
        retries = max(0, _env_int("CLAIMS_SILVER_RELATION_BATCH_RETRIES", 1))
        fallback_limit = max(0, _env_int("CLAIMS_SILVER_RELATION_FALLBACK_MAX_CALLS", 256))
        wall_timeout = max(0.0, _env_float("CLAIMS_SILVER_RELATION_WALL_TIMEOUT", 900.0))
        deadline = time.monotonic() + wall_timeout if wall_timeout else None
        batches = _pack_relation_batches(
            pairs,
            max_items=self.batch_size,
            input_token_limit=input_token_limit,
        )
        pending: list[tuple[list[tuple[ComparisonCandidate, ComparisonCandidate]], int, int, bool]] = [
            (batch, 0, 0, False) for batch in batches
        ]
        completed: dict[tuple[str, str], CandidatePairEdge] = {}
        fallback_calls = 0
        LOGGER.info(
            "Silver relation schedule pairs=%d batches=%d workers=%d batch_size=%d "
            "input_token_limit=%d wall_timeout=%.1fs",
            len(pairs),
            len(batches),
            min(self.max_workers, len(batches)),
            self.batch_size,
            input_token_limit,
            wall_timeout,
        )

        while pending:
            LOGGER.info(
                "Silver relation wave pending_batches=%d fallback_calls=%d fallback_limit=%d",
                len(pending),
                fallback_calls,
                fallback_limit,
            )
            if deadline is not None and time.monotonic() >= deadline:
                for batch, _attempt, _depth, _fallback in pending:
                    _record_relation_fallbacks(
                        completed,
                        batch,
                        "relation wall-clock deadline exceeded",
                        label=self.fallback_label,
                    )
                break
            runnable = []
            for job in pending:
                batch, _attempt, _depth, fallback = job
                if fallback and fallback_limit and fallback_calls >= fallback_limit:
                    _record_relation_fallbacks(
                        completed,
                        batch,
                        "relation fallback call budget exhausted",
                        label=self.fallback_label,
                    )
                    continue
                runnable.append(job)
                if fallback:
                    fallback_calls += 1
            if not runnable:
                break
            executor = ThreadPoolExecutor(
                max_workers=min(self.max_workers, len(runnable)),
                thread_name_prefix="claims-relation",
            )
            request_timeout = None if deadline is None else max(0.001, deadline - time.monotonic())
            futures: dict[
                Future[tuple[list[CandidatePairEdge], list[tuple[ComparisonCandidate, ComparisonCandidate]], str]],
                tuple[list[tuple[ComparisonCandidate, ComparisonCandidate]], int, int, bool],
            ] = {
                executor.submit(self._classify_batch_attempt, batch, timeout_seconds=request_timeout): job
                for job in runnable
                for batch in [job[0]]
            }
            timeout = None if deadline is None else max(0.0, deadline - time.monotonic())
            done, not_done = wait(futures, timeout=timeout)
            next_pending = []
            for future in done:
                batch, attempt, depth, _fallback = futures[future]
                try:
                    edges, failed_pairs, reason = future.result()
                except Exception as exc:  # pragma: no cover - defensive around custom programs
                    edges = []
                    failed_pairs = batch
                    reason = f"{type(exc).__name__}: {exc}"
                for edge in edges:
                    completed[_pair_key_from_edge(edge)] = edge
                if not failed_pairs:
                    continue
                if attempt < retries:
                    LOGGER.warning(
                        "Retrying Silver relation batch unresolved=%d/%d attempt=%d reason=%s",
                        len(failed_pairs),
                        len(batch),
                        attempt + 1,
                        reason,
                    )
                    next_pending.append((failed_pairs, attempt + 1, depth, True))
                elif len(failed_pairs) > 1:
                    midpoint = max(1, len(failed_pairs) // 2)
                    LOGGER.warning(
                        "Splitting failed Silver relation batch unresolved=%d depth=%d reason=%s",
                        len(failed_pairs),
                        depth + 1,
                        reason,
                    )
                    next_pending.extend(
                        (split, 0, depth + 1, True)
                        for split in (failed_pairs[:midpoint], failed_pairs[midpoint:])
                        if split
                    )
                else:
                    _record_relation_fallbacks(completed, failed_pairs, reason, label=self.fallback_label)
            for future in not_done:
                future.cancel()
                batch, _attempt, _depth, _fallback = futures[future]
                _record_relation_fallbacks(
                    completed,
                    batch,
                    "relation wall-clock deadline exceeded",
                    label=self.fallback_label,
                )
            executor.shutdown(wait=not not_done, cancel_futures=True)
            if not_done:
                break
            pending = next_pending

        return [
            completed.get(_pair_key(left, right))
            or self._batch_fallback(left, right, "relation result missing after batch execution")
            for left, right in pairs
        ]

    def _classify_batch_attempt(
        self,
        batch: list[tuple[ComparisonCandidate, ComparisonCandidate]],
        *,
        timeout_seconds: float | None = None,
    ) -> tuple[
        list[CandidatePairEdge],
        list[tuple[ComparisonCandidate, ComparisonCandidate]],
        str,
    ]:
        started = time.perf_counter()
        operation_id = hashlib.sha256(
            "|".join(f"{left.candidate_id}::{right.candidate_id}" for left, right in batch).encode("utf-8")
        ).hexdigest()[:12]
        LOGGER.info(
            "Silver relation batch start operation=%s pairs=%d timeout=%s",
            operation_id,
            len(batch),
            f"{timeout_seconds:.1f}s" if timeout_seconds is not None else "none",
        )
        try:
            if len(batch) == 1:
                left, right = batch[0]
                payload = self._classify_with_dspy(left, right, timeout_seconds=timeout_seconds)
                edges = [
                    CandidatePairEdge(
                        edge_id=f"{left.candidate_id}::{right.candidate_id}",
                        left_candidate_id=left.candidate_id,
                        right_candidate_id=right.candidate_id,
                        relation=_coerce_relation(payload.get("relation")),
                        confidence=_clamp_float(payload.get("confidence"), default=0.0),
                        rationale=str(payload.get("rationale") or ""),
                    )
                ]
                failed_pairs: list[tuple[ComparisonCandidate, ComparisonCandidate]] = []
            else:
                payload = self._classify_batch_with_dspy(batch, timeout_seconds=timeout_seconds)
                results = payload.get("results") if isinstance(payload.get("results"), list) else []
                results_by_pair = {
                    (str(item.get("left_candidate_id") or ""), str(item.get("right_candidate_id") or "")): item
                    for item in results
                    if isinstance(item, dict)
                }
                edges = []
                failed_pairs = []
                for left, right in batch:
                    result = results_by_pair.get((left.candidate_id, right.candidate_id))
                    if result is None:
                        failed_pairs.append((left, right))
                        continue
                    edges.append(
                        CandidatePairEdge(
                            edge_id=f"{left.candidate_id}::{right.candidate_id}",
                            left_candidate_id=left.candidate_id,
                            right_candidate_id=right.candidate_id,
                            relation=_coerce_relation(result.get("relation")),
                            confidence=_clamp_float(result.get("confidence"), default=0.0),
                            rationale=str(result.get("rationale") or ""),
                        )
                    )
            reason = "missing batch result" if failed_pairs else ""
        except Exception as exc:
            edges = []
            failed_pairs = batch
            reason = f"{type(exc).__name__}: {exc}"
        LOGGER.info(
            "Silver relation batch finish operation=%s pairs=%d failures=%d elapsed=%.3fs",
            operation_id,
            len(batch),
            len(failed_pairs),
            time.perf_counter() - started,
        )
        return edges, failed_pairs, reason

    def _classify_batch(
        self,
        batch: list[tuple[ComparisonCandidate, ComparisonCandidate]],
    ) -> list[CandidatePairEdge]:
        if len(batch) == 1:
            left, right = batch[0]
            return [self(left, right)]
        try:
            payload = self._classify_batch_with_dspy(batch)
            results = payload.get("results") if isinstance(payload.get("results"), list) else []
            results_by_pair = {
                (str(item.get("left_candidate_id") or ""), str(item.get("right_candidate_id") or "")): item
                for item in results
                if isinstance(item, dict)
            }
            edges: list[CandidatePairEdge] = []
            for left, right in batch:
                result = results_by_pair.get((left.candidate_id, right.candidate_id))
                if result is None:
                    edges.append(self._batch_fallback(left, right, "missing batch result"))
                    continue
                edges.append(
                    CandidatePairEdge(
                        edge_id=f"{left.candidate_id}::{right.candidate_id}",
                        left_candidate_id=left.candidate_id,
                        right_candidate_id=right.candidate_id,
                        relation=_coerce_relation(result.get("relation")),
                        confidence=_clamp_float(result.get("confidence"), default=0.0),
                        rationale=str(result.get("rationale") or ""),
                    )
                )
            return edges
        except Exception as exc:
            if not self.fallback_to_heuristic:
                raise
            reason = f"{type(exc).__name__}: {exc}"
            return [self._batch_fallback(left, right, reason) for left, right in batch]

    def _batch_fallback(
        self,
        left: ComparisonCandidate,
        right: ComparisonCandidate,
        reason: str,
    ) -> CandidatePairEdge:
        edge = classify_candidate_pair_heuristic(left, right)
        edge.rationale = f"{edge.rationale} {self.fallback_label} batch classifier fallback: {reason}"
        return edge

    def _classify_batch_with_dspy(
        self,
        pairs: list[tuple[ComparisonCandidate, ComparisonCandidate]],
        *,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        pair_rows = [
            {
                "left_candidate_id": left.candidate_id,
                "right_candidate_id": right.candidate_id,
                "left_candidate": _candidate_payload(left),
                "right_candidate": _candidate_payload(right),
            }
            for left, right in pairs
        ]
        kwargs = {
            "task_instructions": _relation_batch_instructions(),
            "candidate_pairs_json": json.dumps(pair_rows, ensure_ascii=False),
            "allowed_relations_json": json.dumps(sorted(ALLOWED_RELATIONS), ensure_ascii=False),
            "required_json_schema": json.dumps(
                {
                    "results": [
                        {
                            "left_candidate_id": "exact input id",
                            "right_candidate_id": "exact input id",
                            "relation": "one allowed relation",
                            "confidence": "number from 0 to 1",
                            "rationale": "short explanation",
                        }
                    ]
                },
                ensure_ascii=False,
            ),
        }
        started_at = datetime.now(timezone.utc)
        started = time.perf_counter()
        usage = empty_usage("dspy_program_unavailable")
        status = "success"
        error = None
        lm = None
        raw = ""
        try:
            with _request_slot(self.request_gate):
                if self._provided_batch_program is not None:
                    prediction = self._provided_batch_program(**kwargs)
                else:
                    program = self._build_batch_program()
                    lm = self._new_lm(
                        max_tokens=max(
                            self.max_tokens,
                            _env_int("CLAIMS_SILVER_RELATION_BATCH_MAX_TOKENS", 8192),
                        ),
                        timeout_seconds=timeout_seconds,
                    )
                    if hasattr(self._dspy_module, "context"):
                        with self._dspy_module.context(lm=lm):
                            prediction = program(**kwargs)
                    else:  # pragma: no cover - retained for older DSPy versions
                        self._dspy_module.configure(lm=lm)
                        prediction = program(**kwargs)
            raw = str(getattr(prediction, "relations_json", prediction if isinstance(prediction, str) else ""))
            return _parse_json_object(raw)
        except Exception as exc:
            status = "failed"
            error = f"{type(exc).__name__}: {exc}"
            _write_relation_failure(
                pairs=pairs,
                stage_key=self.stage_key,
                model=self.model,
                raw_response=raw,
                error=error,
            )
            raise
        finally:
            if lm is not None:
                usage = usage_from_dspy_lm(lm)
                _close_dspy_lm(lm)
            if self.usage_sink is not None:
                pair_ids = [f"{left.candidate_id}::{right.candidate_id}" for left, right in pairs]
                digest = hashlib.sha256("|".join(pair_ids).encode("utf-8")).hexdigest()[:16]
                self.usage_sink(
                    {
                        "paper_id": next(
                            (left.paper_id or right.paper_id for left, right in pairs if left.paper_id or right.paper_id),
                            None,
                        ),
                        "stage_key": self.stage_key,
                        "stage_label": self.stage_label,
                        "role": "relation_classifier",
                        "operation_id": f"relation_batch:{digest}",
                        "harness": "dspy",
                        "runtime": "dspy-predict-batch",
                        "provider": provider_from_model_or_base(self.model, self.api_base),
                        "model": self.model,
                        "usage": usage,
                        "status": status,
                        "error": error,
                        "started_at": started_at,
                        "ended_at": datetime.now(timezone.utc),
                        "duration_seconds": time.perf_counter() - started,
                        "metadata": {
                            "batch": True,
                            "pair_count": len(pairs),
                            "pair_ids": pair_ids,
                        },
                    }
                )

    def _classify_with_dspy(
        self,
        left: ComparisonCandidate,
        right: ComparisonCandidate,
        *,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        kwargs = {
            "task_instructions": _relation_classifier_instructions(),
            "left_candidate_json": json.dumps(_candidate_payload(left), ensure_ascii=False, indent=2),
            "right_candidate_json": json.dumps(_candidate_payload(right), ensure_ascii=False, indent=2),
            "allowed_relations_json": json.dumps(sorted(ALLOWED_RELATIONS), ensure_ascii=False),
        }
        started_at = datetime.now(timezone.utc)
        started = time.perf_counter()
        usage = empty_usage("dspy_program_unavailable")
        status = "success"
        error = None
        lm = None
        raw = ""
        try:
            with _request_slot(self.request_gate):
                if self._program is not None:
                    prediction = self._program(**kwargs)
                else:
                    program = self._build_program()
                    lm = self._new_lm(timeout_seconds=timeout_seconds)
                    if hasattr(self._dspy_module, "context"):
                        with self._dspy_module.context(lm=lm):
                            prediction = program(**kwargs)
                    else:  # pragma: no cover - retained for older DSPy versions
                        self._dspy_module.configure(lm=lm)
                        prediction = program(**kwargs)
            raw = str(getattr(prediction, "relation_json", prediction if isinstance(prediction, str) else ""))
            return _parse_json_object(raw)
        except Exception as exc:
            status = "failed"
            error = f"{type(exc).__name__}: {exc}"
            _write_relation_failure(
                pairs=[(left, right)],
                stage_key=self.stage_key,
                model=self.model,
                raw_response=raw,
                error=error,
            )
            raise
        finally:
            if lm is not None:
                usage = usage_from_dspy_lm(lm)
                _close_dspy_lm(lm)
            if self.usage_sink is not None:
                paper_id = left.paper_id or right.paper_id
                edge_id = f"{left.candidate_id}::{right.candidate_id}"
                self.usage_sink(
                    {
                        "paper_id": paper_id,
                        "stage_key": self.stage_key,
                        "stage_label": self.stage_label,
                        "role": "relation_classifier",
                        "operation_id": edge_id,
                        "harness": "dspy",
                        "runtime": "dspy-predict",
                        "provider": provider_from_model_or_base(self.model, self.api_base),
                        "model": self.model,
                        "usage": usage,
                        "status": status,
                        "error": error,
                        "started_at": started_at,
                        "ended_at": datetime.now(timezone.utc),
                        "duration_seconds": time.perf_counter() - started,
                        "metadata": {
                            "left_candidate_id": left.candidate_id,
                            "right_candidate_id": right.candidate_id,
                        },
                    }
                )

    def _build_program(self):
        with self._program_lock:
            if self._base_program is not None:
                return self._base_program
            if self._program is not None:
                return self._program
            dspy_module = self._dspy_module
            if dspy_module is None:
                try:
                    import dspy as imported_dspy
                except ImportError as exc:  # pragma: no cover - depends on local install
                    raise RuntimeError("dspy is required for DSPy relation classification.") from exc
                dspy_module = imported_dspy
                self._dspy_module = dspy_module
            if not self.api_key:
                raise RuntimeError("OPENROUTER_API_KEY is required for DSPy relation classification.")

            class RelationSignature(dspy_module.Signature):
                """Classify the relation between two extracted scientific claim candidates. Return strict JSON only."""

                task_instructions: str = dspy_module.InputField()
                left_candidate_json: str = dspy_module.InputField()
                right_candidate_json: str = dspy_module.InputField()
                allowed_relations_json: str = dspy_module.InputField()
                relation_json: str = dspy_module.OutputField(desc="Strict JSON object with relation, confidence, and rationale.")

            self._base_program = dspy_module.Predict(RelationSignature)
            return self._base_program

    def _build_batch_program(self):
        with self._program_lock:
            if self._batch_program is not None:
                return self._batch_program
            dspy_module = self._dspy_module
            if dspy_module is None:
                try:
                    import dspy as imported_dspy
                except ImportError as exc:  # pragma: no cover - depends on local install
                    raise RuntimeError("dspy is required for DSPy relation classification.") from exc
                dspy_module = imported_dspy
                self._dspy_module = dspy_module
            if not self.api_key:
                raise RuntimeError("OPENROUTER_API_KEY is required for DSPy relation classification.")

            class BatchRelationSignature(dspy_module.Signature):
                """Classify a batch of scientific claim pairs. Return strict JSON only."""

                task_instructions: str = dspy_module.InputField()
                candidate_pairs_json: str = dspy_module.InputField()
                allowed_relations_json: str = dspy_module.InputField()
                required_json_schema: str = dspy_module.InputField()
                relations_json: str = dspy_module.OutputField(desc="Strict JSON object matching required_json_schema.")

            self._batch_program = dspy_module.Predict(BatchRelationSignature)
            return self._batch_program

    def _new_lm(
        self,
        *,
        max_tokens: int | None = None,
        timeout_seconds: float | None = None,
    ):
        return self._dspy_module.LM(
            model=self.model,
            api_key=self.api_key,
            api_base=self.api_base,
            temperature=self.temperature,
            max_tokens=max_tokens if max_tokens is not None else self.max_tokens,
            timeout=_bounded_timeout(self.timeout_seconds, timeout_seconds),
            # Visible retry/split policy lives in classify_many.
            num_retries=0,
        )


class OpenAICompatibleRelationClassifier(DSPyRelationClassifier):
    """Relation classifier using a direct OpenAI-compatible chat endpoint."""

    def __init__(
        self,
        *,
        completion_fn: Callable[[list[dict[str, str]]], str] | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(fallback_label="OpenAI-compatible", **kwargs)
        self.completion_fn = completion_fn

    def _classify_batch_with_dspy(
        self,
        pairs: list[tuple[ComparisonCandidate, ComparisonCandidate]],
        *,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        return self._classify_with_openai(pairs, batch=True, timeout_seconds=timeout_seconds)

    def _classify_with_dspy(
        self,
        left: ComparisonCandidate,
        right: ComparisonCandidate,
        *,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        return self._classify_with_openai(
            [(left, right)],
            batch=False,
            timeout_seconds=timeout_seconds,
        )

    def _classify_with_openai(
        self,
        pairs: list[tuple[ComparisonCandidate, ComparisonCandidate]],
        *,
        batch: bool,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        payload = _relation_request_payload(pairs, batch=batch)
        messages = [
            {
                "role": "system",
                "content": _relation_batch_instructions() if batch else _relation_classifier_instructions(),
            },
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ]
        started_at = datetime.now(timezone.utc)
        started = time.perf_counter()
        usage = empty_usage("completion_fn_unavailable")
        status = "success"
        error = None
        raw = ""
        try:
            with _request_slot(self.request_gate):
                if self.completion_fn is not None:
                    raw = self.completion_fn(messages)
                else:
                    raw, usage = self._complete_openai(messages, timeout_seconds=timeout_seconds)
            return _parse_json_object(raw)
        except Exception as exc:
            status = "failed"
            error = f"{type(exc).__name__}: {exc}"
            _write_relation_failure(
                pairs=pairs,
                stage_key=self.stage_key,
                model=self.model,
                raw_response=raw,
                error=error,
            )
            raise
        finally:
            _emit_external_relation_usage(
                self,
                pairs=pairs,
                harness="openai-compatible",
                runtime="openai-compatible-chat",
                usage=usage,
                status=status,
                error=error,
                started_at=started_at,
                duration_seconds=time.perf_counter() - started,
                batch=batch,
            )

    def _complete_openai(
        self,
        messages: list[dict[str, str]],
        *,
        timeout_seconds: float | None = None,
    ) -> tuple[str, dict[str, Any]]:
        endpoint = f"{self.api_base.rstrip('/')}/chat/completions"
        request = urllib.request.Request(
            endpoint,
            data=json.dumps(
                {
                    "model": self.model.removeprefix("openrouter/"),
                    "messages": messages,
                    "temperature": self.temperature,
                    "max_tokens": max(
                        self.max_tokens,
                        _env_int("CLAIMS_SILVER_RELATION_BATCH_MAX_TOKENS", 8192),
                    ),
                    "response_format": {"type": "json_object"},
                }
            ).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(
                request,
                timeout=_bounded_timeout(self.timeout_seconds, timeout_seconds),
            ) as response:
                response_payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"relation endpoint returned HTTP {exc.code}: {body[:500]}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"relation endpoint unavailable: {exc.reason}") from exc
        choices = response_payload.get("choices") if isinstance(response_payload, dict) else None
        message = choices[0].get("message") if choices and isinstance(choices[0], dict) else None
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, str) or not content.strip():
            raise ValueError("relation endpoint response did not include message content")
        return content, _usage_from_openai_response(response_payload)


class CLIRelationClassifier(DSPyRelationClassifier):
    """Relation classifier for CLI wrappers that accept stdin or a prompt-file placeholder."""

    def __init__(self, *, command: list[str] | str, **kwargs: Any) -> None:
        super().__init__(fallback_label="CLI", **kwargs)
        self.command = shlex.split(command) if isinstance(command, str) else list(command)
        if not self.command:
            raise ValueError("Silver relation CLI command is empty")

    def _classify_batch_with_dspy(
        self,
        pairs: list[tuple[ComparisonCandidate, ComparisonCandidate]],
        *,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        return self._classify_with_cli(pairs, batch=True, timeout_seconds=timeout_seconds)

    def _classify_with_dspy(
        self,
        left: ComparisonCandidate,
        right: ComparisonCandidate,
        *,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        return self._classify_with_cli(
            [(left, right)],
            batch=False,
            timeout_seconds=timeout_seconds,
        )

    def _classify_with_cli(
        self,
        pairs: list[tuple[ComparisonCandidate, ComparisonCandidate]],
        *,
        batch: bool,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        prompt = "\n\n".join(
            [
                "# Claims Silver relation classification",
                _relation_batch_instructions() if batch else _relation_classifier_instructions(),
                "Return only one strict JSON object.",
                json.dumps(_relation_request_payload(pairs, batch=batch), ensure_ascii=False),
            ]
        )
        command = list(self.command)
        prompt_path: Path | None = None
        stdin: str | None = prompt
        if any("{prompt_file}" in argument for argument in command):
            descriptor, path_value = tempfile.mkstemp(prefix="claims-relation-", suffix=".txt", text=True)
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(prompt)
            prompt_path = Path(path_value)
            command = [argument.replace("{prompt_file}", str(prompt_path)) for argument in command]
            stdin = None
        started_at = datetime.now(timezone.utc)
        started = time.perf_counter()
        usage = empty_usage("cli_not_started")
        status = "success"
        error = None
        completed: subprocess.CompletedProcess[str] | None = None
        try:
            with _request_slot(self.request_gate):
                completed = subprocess.run(
                    command,
                    input=stdin,
                    capture_output=True,
                    text=True,
                    timeout=_bounded_timeout(self.timeout_seconds, timeout_seconds),
                    check=False,
                )
            usage = usage_from_cli_process(command, completed.stdout, completed.stderr)
            if completed.returncode != 0:
                raise RuntimeError(f"relation CLI exited {completed.returncode}: {completed.stderr[-1000:]}")
            return _parse_json_object(completed.stdout)
        except Exception as exc:
            status = "failed"
            error = f"{type(exc).__name__}: {exc}"
            _write_relation_failure(
                pairs=pairs,
                stage_key=self.stage_key,
                model=self.model,
                raw_response=completed.stdout if completed is not None else "",
                error=error,
            )
            raise
        finally:
            if prompt_path is not None:
                prompt_path.unlink(missing_ok=True)
            _emit_external_relation_usage(
                self,
                pairs=pairs,
                harness="cli",
                runtime=Path(command[0]).name if command else "cli",
                usage=usage,
                status=status,
                error=error,
                started_at=started_at,
                duration_seconds=time.perf_counter() - started,
                batch=batch,
            )


def _relation_request_payload(
    pairs: list[tuple[ComparisonCandidate, ComparisonCandidate]],
    *,
    batch: bool,
) -> dict[str, Any]:
    rows = [
        {
            "left_candidate_id": left.candidate_id,
            "right_candidate_id": right.candidate_id,
            "left_candidate": _candidate_payload(left),
            "right_candidate": _candidate_payload(right),
        }
        for left, right in pairs
    ]
    schema = {
        "results": [
            {
                "left_candidate_id": "exact input id",
                "right_candidate_id": "exact input id",
                "relation": "one allowed relation",
                "confidence": "number from 0 to 1",
                "rationale": "short explanation",
            }
        ]
    }
    if batch:
        return {"candidate_pairs": rows, "allowed_relations": sorted(ALLOWED_RELATIONS), "required_schema": schema}
    row = rows[0]
    return {
        "left_candidate": row["left_candidate"],
        "right_candidate": row["right_candidate"],
        "allowed_relations": sorted(ALLOWED_RELATIONS),
        "required_schema": {
            "relation": "one allowed relation",
            "confidence": "number from 0 to 1",
            "rationale": "short explanation",
        },
    }


def _emit_external_relation_usage(
    classifier: DSPyRelationClassifier,
    *,
    pairs: list[tuple[ComparisonCandidate, ComparisonCandidate]],
    harness: str,
    runtime: str,
    usage: dict[str, Any],
    status: str,
    error: str | None,
    started_at: datetime,
    duration_seconds: float,
    batch: bool,
) -> None:
    if classifier.usage_sink is None:
        return
    pair_ids = [f"{left.candidate_id}::{right.candidate_id}" for left, right in pairs]
    digest = hashlib.sha256("|".join(pair_ids).encode("utf-8")).hexdigest()[:16]
    classifier.usage_sink(
        {
            "paper_id": next(
                (left.paper_id or right.paper_id for left, right in pairs if left.paper_id or right.paper_id),
                None,
            ),
            "stage_key": classifier.stage_key,
            "stage_label": classifier.stage_label,
            "role": "relation_classifier",
            "operation_id": f"relation_{'batch' if batch else 'pair'}:{digest}",
            "harness": harness,
            "runtime": runtime,
            "provider": provider_from_model_or_base(classifier.model, classifier.api_base),
            "model": classifier.model,
            "usage": usage,
            "status": status,
            "error": error,
            "started_at": started_at,
            "ended_at": datetime.now(timezone.utc),
            "duration_seconds": duration_seconds,
            "metadata": {"batch": batch, "pair_count": len(pairs), "pair_ids": pair_ids},
        }
    )


def _usage_from_openai_response(response_payload: dict[str, Any]) -> dict[str, Any]:
    usage = response_payload.get("usage") if isinstance(response_payload.get("usage"), dict) else {}
    prompt_details = usage.get("prompt_tokens_details") if isinstance(usage.get("prompt_tokens_details"), dict) else {}
    completion_details = usage.get("completion_tokens_details") if isinstance(usage.get("completion_tokens_details"), dict) else {}
    cost = usage.get("cost") if isinstance(usage.get("cost"), int | float) else None
    return {
        "prompt_tokens": _optional_int(usage.get("prompt_tokens")),
        "completion_tokens": _optional_int(usage.get("completion_tokens")),
        "reasoning_tokens": _optional_int(completion_details.get("reasoning_tokens")),
        "cache_read_tokens": _optional_int(prompt_details.get("cached_tokens")),
        "cache_write_tokens": None,
        "total_tokens": _optional_int(usage.get("total_tokens")),
        "cost_usd": float(cost) if cost is not None else None,
        "cost_kind": "actual" if cost is not None else "unavailable",
        "source": "openai_compatible_response",
    }


def _optional_int(value: Any) -> int | None:
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    return None


def _similarity(left: str, right: str) -> float:
    if not left or not right:
        return 0.0
    if left in right or right in left:
        return max(SequenceMatcher(None, left, right).ratio(), 0.82)
    return SequenceMatcher(None, left, right).ratio()


def _contains_numbers(value: str) -> bool:
    return any(char.isdigit() for char in value)


def _numbers(value: str) -> list[str]:
    return [part for part in value.split() if any(char.isdigit() for char in part)]


def _candidate_payload(candidate: ComparisonCandidate) -> dict[str, Any]:
    return {
        "candidate_id": candidate.candidate_id,
        "origin": candidate.origin,
        "miner_id": candidate.miner_id,
        "record_id": candidate.record_id,
        "statement": candidate.statement,
        "qualifier": candidate.qualifier,
        "source_span_ids": candidate.source_span_ids,
        "source_quotes": candidate.source_quotes[:3],
        "evidence_ids": candidate.evidence_ids,
        "evidence_records": candidate.metadata.get("evidence_records", [])[:3]
        if isinstance(candidate.metadata.get("evidence_records"), list)
        else [],
    }


def _relation_classifier_instructions() -> str:
    return """
You classify whether two extracted scientific claim records refer to the same underlying paper claim.

Use only the candidate statements, qualifiers, linked evidence records, source span IDs, and source quotes provided.

Return one JSON object:
{
  "relation": one of allowed_relations_json,
  "confidence": number from 0 to 1,
  "rationale": short explanation
}

Relation meanings:
- semantic_equivalent: same scientific claim; wording differences do not change scope, numeric content, method, population, or evidence.
- compatible_refinement: same core claim, but one candidate is more specific or better qualified without contradicting the other.
- compatible_split_merge: candidates differ because one splits/merges a claim, but the information is compatible.
- partial_overlap: candidates share topic/evidence but are not safely the same claim.
- contradiction: candidates make incompatible assertions, numeric values, directions, populations, methods, or scope.
- unrelated: candidates are about different scientific units.
- insufficient_context: cannot decide from provided candidate/evidence text.

Prefer partial_overlap over semantic_equivalent when an important field is missing or softened.
Prefer compatible_refinement when the difference is an added valid qualifier or detail.
Prefer contradiction for material numeric/scope/method disagreement.
""".strip()


def _relation_batch_instructions() -> str:
    return (
        _relation_classifier_instructions()
        + "\n\nClassify every input pair independently. Return exactly one result for every pair, "
        "preserving both candidate IDs exactly. Return only the required JSON object."
    )


def _coerce_relation(value: Any) -> RelationType:
    normalized = str(value or "").strip().lower()
    if normalized in ALLOWED_RELATIONS:
        return normalized  # type: ignore[return-value]
    return "insufficient_context"


def _clamp_float(value: Any, *, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return round(min(1.0, max(0.0, parsed)), 4)


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


def _bounded_timeout(default_timeout: float, remaining_timeout: float | None) -> float:
    default_value = float(default_timeout or 0.0)
    if remaining_timeout is None:
        return default_value
    remaining_value = max(0.001, float(remaining_timeout))
    if default_value <= 0:
        return remaining_value
    return min(default_value, remaining_value)


def _pack_relation_batches(
    pairs: list[tuple[ComparisonCandidate, ComparisonCandidate]],
    *,
    max_items: int,
    input_token_limit: int,
) -> list[list[tuple[ComparisonCandidate, ComparisonCandidate]]]:
    max_items = max(1, int(max_items or 1))
    if input_token_limit <= 0:
        return [pairs[start : start + max_items] for start in range(0, len(pairs), max_items)]
    batches: list[list[tuple[ComparisonCandidate, ComparisonCandidate]]] = []
    current: list[tuple[ComparisonCandidate, ComparisonCandidate]] = []
    current_tokens = 0
    for pair in pairs:
        estimated_tokens = _estimate_pair_tokens(*pair)
        if current and (len(current) >= max_items or current_tokens + estimated_tokens > input_token_limit):
            batches.append(current)
            current = []
            current_tokens = 0
        current.append(pair)
        current_tokens += estimated_tokens
        if len(current) >= max_items:
            batches.append(current)
            current = []
            current_tokens = 0
    if current:
        batches.append(current)
    return batches


def _estimate_pair_tokens(left: ComparisonCandidate, right: ComparisonCandidate) -> int:
    payload = {
        "left_candidate": _candidate_payload(left),
        "right_candidate": _candidate_payload(right),
    }
    return max(1, (len(json.dumps(payload, ensure_ascii=False)) + 2) // 3)


def _pair_key(
    left: ComparisonCandidate,
    right: ComparisonCandidate,
) -> tuple[str, str]:
    return tuple(sorted((left.candidate_id, right.candidate_id)))


def _pair_key_from_edge(edge: CandidatePairEdge) -> tuple[str, str]:
    return tuple(sorted((edge.left_candidate_id, edge.right_candidate_id)))


def _record_relation_fallbacks(
    completed: dict[tuple[str, str], CandidatePairEdge],
    pairs: list[tuple[ComparisonCandidate, ComparisonCandidate]],
    reason: str,
    *,
    label: str,
) -> None:
    for left, right in pairs:
        edge = classify_candidate_pair_heuristic(left, right)
        edge.rationale = f"{edge.rationale} {label} classifier fallback: {reason}"
        completed.setdefault(_pair_key(left, right), edge)


def _parse_json_object(value: str) -> dict[str, Any]:
    text = value.strip()
    if not text:
        raise ValueError("relation classifier returned empty output")
    candidates = [text, *_json_object_candidates(text)]
    parsed_objects: list[dict[str, Any]] = []
    last_error: Exception | None = None
    index = 0
    while index < len(candidates):
        candidate = candidates[index]
        index += 1
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError as exc:
            last_error = exc
            continue
        if not isinstance(parsed, dict):
            continue
        parsed_objects.append(parsed)
        for key in ("content", "output", "final", "response", "result", "message"):
            nested = parsed.get(key)
            if isinstance(nested, str) and "{" in nested:
                candidates.extend(_json_object_candidates(nested) or [nested])
            elif isinstance(nested, dict):
                parsed_objects.append(nested)
    for parsed in reversed(parsed_objects):
        if isinstance(parsed.get("results"), list):
            return parsed
    for parsed in reversed(parsed_objects):
        if "relation" in parsed:
            return parsed
    if parsed_objects:
        return parsed_objects[-1]
    if last_error is not None:
        raise last_error
    raise ValueError("relation classifier returned non-object JSON")


def _json_object_candidates(text: str) -> list[str]:
    candidates: list[str] = []
    for start, char in enumerate(text):
        if char != "{":
            continue
        depth = 0
        in_string = False
        escaped = False
        for index in range(start, len(text)):
            current = text[index]
            if in_string:
                if escaped:
                    escaped = False
                elif current == "\\":
                    escaped = True
                elif current == '"':
                    in_string = False
                continue
            if current == '"':
                in_string = True
            elif current == "{":
                depth += 1
            elif current == "}":
                depth -= 1
                if depth == 0:
                    candidates.append(text[start : index + 1])
                    break
    return candidates


def _write_relation_failure(
    *,
    pairs: list[tuple[ComparisonCandidate, ComparisonCandidate]],
    stage_key: str,
    model: str,
    raw_response: str,
    error: str,
) -> None:
    configured = os.getenv("CLAIMS_SILVER_RELATION_DEBUG_DIR", "").strip()
    root = Path(configured).expanduser() if configured else Path(tempfile.gettempdir()) / "claims-silver-relation-debug"
    root.mkdir(parents=True, exist_ok=True)
    pair_ids = [f"{left.candidate_id}::{right.candidate_id}" for left, right in pairs]
    digest = hashlib.sha256("|".join(pair_ids).encode("utf-8")).hexdigest()[:16]
    path = root / f"{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S_%f')}_{stage_key}_{digest}.json"
    path.write_text(
        json.dumps(
            {
                "stage_key": stage_key,
                "model": model,
                "pair_ids": pair_ids,
                "pair_count": len(pairs),
                "pairs": [
                    {"left": _candidate_payload(left), "right": _candidate_payload(right)}
                    for left, right in pairs
                ],
                "raw_response": raw_response,
                "error": error,
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    path.chmod(0o600)


def _close_dspy_lm(lm: Any) -> None:
    for target in (
        lm,
        getattr(lm, "client", None),
    ):
        close = getattr(target, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                continue


@contextmanager
def _request_slot(request_gate: Any | None):
    if request_gate is None:
        yield
        return
    request_gate.acquire()
    try:
        yield
    finally:
        request_gate.release()
