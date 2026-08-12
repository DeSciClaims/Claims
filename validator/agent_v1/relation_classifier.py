from __future__ import annotations

import json
import os
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from difflib import SequenceMatcher
from typing import Any

from miner.agent_v1.runtime.usage import empty_usage, usage_from_dspy_lm

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
        dspy_module: Any | None = None,
        program: Any | None = None,
        fallback_to_heuristic: bool = True,
        request_gate: Any | None = None,
        usage_sink: UsageSink | None = None,
        stage_key: str = "silver_comparison",
        stage_label: str = "Comparison graph",
    ) -> None:
        self.model = model or os.getenv("CLAIMS_SILVER_RELATION_MODEL") or os.getenv("OPENROUTER_MODEL", "openrouter/openai/gpt-4o-mini")
        self.api_key = api_key if api_key is not None else os.getenv("OPENROUTER_API_KEY", "")
        self.api_base = api_base or os.getenv("CLAIMS_SILVER_RELATION_API_BASE") or os.getenv("OPENROUTER_API_BASE", "https://openrouter.ai/api/v1")
        self.temperature = temperature if temperature is not None else float(os.getenv("CLAIMS_SILVER_RELATION_TEMPERATURE", "1.0"))
        self.max_tokens = max_tokens if max_tokens is not None else int(os.getenv("CLAIMS_SILVER_RELATION_MAX_TOKENS", "2048"))
        self.timeout_seconds = float(os.getenv("CLAIMS_SILVER_RELATION_TIMEOUT", "120"))
        self._dspy_module = dspy_module
        self._program = program
        self._base_program = None
        self._program_lock = threading.Lock()
        self.fallback_to_heuristic = fallback_to_heuristic
        self.request_gate = request_gate
        self.usage_sink = usage_sink
        self.stage_key = stage_key
        self.stage_label = stage_label

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
            edge.rationale = f"{edge.rationale} DSPy classifier fallback: {type(exc).__name__}: {exc}"
            return edge

    def _classify_with_dspy(self, left: ComparisonCandidate, right: ComparisonCandidate) -> dict[str, Any]:
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
        try:
            with _request_slot(self.request_gate):
                if self._program is not None:
                    prediction = self._program(**kwargs)
                else:
                    program = self._build_program()
                    lm = self._new_lm()
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
            raise
        finally:
            if lm is not None:
                usage = usage_from_dspy_lm(lm)
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

    def _new_lm(self):
        return self._dspy_module.LM(
            model=self.model,
            api_key=self.api_key,
            api_base=self.api_base,
            temperature=self.temperature,
            max_tokens=self.max_tokens,
            timeout=self.timeout_seconds,
            num_retries=_env_int("CLAIMS_SILVER_RELATION_RETRIES", 1),
        )


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


def _parse_json_object(value: str) -> dict[str, Any]:
    text = value.strip()
    if not text:
        raise ValueError("relation classifier returned empty output")
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            raise
        parsed = json.loads(text[start : end + 1])
    if not isinstance(parsed, dict):
        raise ValueError("relation classifier returned non-object JSON")
    return parsed


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
