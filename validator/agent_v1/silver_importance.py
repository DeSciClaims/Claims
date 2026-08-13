from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol

from .comparison_models import SilverRecord
from .model_usage import UsageSink, provider_from_model_or_base


IMPORTANCE_TAGS = {"central", "supporting", "minor"}


class SilverImportanceClassifier(Protocol):
    def classify(
        self,
        silver_record: SilverRecord,
        *,
        paper_context: dict[str, Any],
        source_context: str,
    ) -> dict[str, dict[str, Any]]:
        """Return importance and relevance decisions keyed by Silver unit ID."""


@dataclass(frozen=True)
class OpenAICompatibleSilverImportanceClassifier:
    model: str
    api_key: str
    api_base: str = "https://openrouter.ai/api/v1"
    temperature: float = 0.0
    max_tokens: int = 8192
    timeout_seconds: float = 120.0
    batch_size: int = 8
    completion_fn: Callable[[list[dict[str, str]]], str] | None = field(default=None, compare=False, repr=False)
    usage_sink: UsageSink | None = field(default=None, compare=False, repr=False)

    def classify(
        self,
        silver_record: SilverRecord,
        *,
        paper_context: dict[str, Any],
        source_context: str,
    ) -> dict[str, dict[str, Any]]:
        if not silver_record.silver_units:
            return {}
        started_at = datetime.now(timezone.utc)
        started = time.perf_counter()
        usage = _empty_usage("completion_fn_unavailable")
        status = "success"
        error = None
        assignments: dict[str, dict[str, Any]] = {}
        batches = [
            silver_record.silver_units[index : index + max(1, self.batch_size)]
            for index in range(0, len(silver_record.silver_units), max(1, self.batch_size))
        ]
        try:
            for units in batches:
                batch_record = silver_record.model_copy(update={"silver_units": units})
                messages = _importance_messages(
                    silver_record=batch_record,
                    paper_context=paper_context,
                    source_context=source_context,
                )
                if self.completion_fn is not None:
                    content = self.completion_fn(messages)
                else:
                    content, batch_usage = self._complete(messages)
                    usage = _merge_usage(usage, batch_usage)
                batch_assignments = _parse_importance_response(content)
                expected_ids = {unit.silver_unit_id for unit in units}
                missing_ids = expected_ids - set(batch_assignments)
                if missing_ids:
                    raise ValueError(
                        "importance response omitted Silver units: " + ", ".join(sorted(missing_ids))
                    )
                assignments.update(batch_assignments)
            return assignments
        except Exception as exc:
            status = "failed"
            error = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            if self.usage_sink is not None:
                self.usage_sink(
                    {
                        "paper_id": silver_record.paper_id,
                        "stage_key": "silver_importance",
                        "stage_label": "Importance tagging",
                        "role": "importance_classifier",
                        "operation_id": silver_record.silver_record_id,
                        "harness": "openai-compatible",
                        "runtime": "openai-compatible-chat-completions",
                        "provider": provider_from_model_or_base(self.model, self.api_base),
                        "model": self.model,
                        "usage": usage,
                        "status": status,
                        "error": error,
                        "started_at": started_at,
                        "ended_at": datetime.now(timezone.utc),
                        "duration_seconds": time.perf_counter() - started,
                        "metadata": {
                            "silver_unit_count": len(silver_record.silver_units),
                            "batch_count": len(batches),
                            "batch_size": max(1, self.batch_size),
                        },
                    }
                )

    def _complete(self, messages: list[dict[str, str]]) -> tuple[str, dict[str, Any]]:
        if not self.api_key:
            raise RuntimeError("Silver importance classifier API key is required.")
        endpoint = f"{self.api_base.rstrip('/')}/chat/completions"
        request_body = json.dumps(
            {
                "model": _direct_api_model_id(self.model, self.api_base),
                "messages": messages,
                "temperature": self.temperature,
                "max_tokens": self.max_tokens,
                "response_format": {"type": "json_object"},
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            endpoint,
            data=request_body,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                response_payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"importance endpoint returned HTTP {exc.code}: {body[:500]}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"importance endpoint unavailable: {exc.reason}") from exc

        choices = response_payload.get("choices") if isinstance(response_payload, dict) else None
        if not choices:
            raise ValueError("importance endpoint response did not include choices")
        message = choices[0].get("message") if isinstance(choices[0], dict) else None
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, str) or not content.strip():
            raise ValueError("importance endpoint response did not include message content")
        return content, _usage_from_response(response_payload)


def apply_silver_importance(
    silver_record: SilverRecord,
    *,
    classifier: SilverImportanceClassifier | None,
    paper_context: dict[str, Any],
    source_context: str,
) -> SilverRecord:
    if classifier is None or not silver_record.silver_units:
        silver_record.metadata["importance_assignment"] = {
            "mode": "default",
            "note": "No Silver importance classifier configured; all units retain their default tag.",
        }
        return silver_record
    try:
        assignments = classifier.classify(
            silver_record,
            paper_context=paper_context,
            source_context=source_context,
        )
    except Exception as exc:
        silver_record.metadata["importance_assignment"] = {
            "mode": "failed",
            "error": f"{type(exc).__name__}: {exc}",
            "note": "Silver scoring was stopped because relevance and importance classification was incomplete.",
        }
        raise RuntimeError("Silver relevance and importance classification failed") from exc

    applied: dict[str, dict[str, Any]] = {}
    retained_units = []
    excluded_units: list[dict[str, Any]] = []
    for unit in silver_record.silver_units:
        assignment = assignments.get(unit.silver_unit_id) or {}
        importance = str(assignment.get("importance") or "").strip().lower()
        if importance not in IMPORTANCE_TAGS:
            raise ValueError(f"Missing valid importance tag for Silver unit {unit.silver_unit_id}")
        include_in_silver = assignment.get("include_in_silver")
        if not isinstance(include_in_silver, bool):
            raise ValueError(f"Missing relevance decision for Silver unit {unit.silver_unit_id}")
        rationale = str(assignment.get("rationale") or "").strip()
        has_bronze_anchor = any(candidate_id.startswith("bronze:") for candidate_id in unit.equivalent_candidate_ids)
        if not include_in_silver and not has_bronze_anchor:
            excluded_units.append(
                {
                    "silver_unit_id": unit.silver_unit_id,
                    "statement": unit.statement,
                    "equivalent_candidate_ids": unit.equivalent_candidate_ids,
                    "rationale": rationale,
                }
            )
            continue
        unit.importance = importance  # type: ignore[assignment]
        unit.metadata["importance_assignment"] = {
            "importance": importance,
            "rationale": rationale,
            "include_in_silver": True,
            "bronze_anchor_override": bool(has_bronze_anchor and not include_in_silver),
            "source": "validator_silver_importance_classifier",
        }
        retained_units.append(unit)
        applied[unit.silver_unit_id] = {
            "importance": importance,
            "rationale": rationale,
            "include_in_silver": True,
        }
    silver_record.silver_units = retained_units
    silver_record.metadata["importance_assignment"] = {
        "mode": "classifier",
        "applied_count": len(applied),
        "input_unit_count": len(assignments),
        "unit_count": len(retained_units),
        "excluded_count": len(excluded_units),
        "excluded_units": excluded_units,
        "assignments": applied,
    }
    return silver_record


def _importance_messages(
    *,
    silver_record: SilverRecord,
    paper_context: dict[str, Any],
    source_context: str,
) -> list[dict[str, str]]:
    payload = {
        "paper": {
            "paper_id": silver_record.paper_id,
            "title": paper_context.get("title") or paper_context.get("paper_title") or "",
            "abstract": paper_context.get("abstract") or paper_context.get("summary") or "",
            "claims_summary": paper_context.get("claims_summary") or "",
        },
        "silver_units": [
            {
                "silver_unit_id": unit.silver_unit_id,
                "statement": unit.statement,
                "scoring_mode": unit.scoring_mode,
                "evidence_ids": unit.evidence_ids,
                "source_span_ids": unit.source_span_ids,
                "source_quotes": unit.source_quotes[:3],
                "evidence_records": unit.metadata.get("evidence_records", [])[:5]
                if isinstance(unit.metadata.get("evidence_records"), list)
                else [],
            }
            for unit in silver_record.silver_units
        ],
        "source_context": source_context[:16000],
        "required_json_schema": {
            "units": [
                {
                    "silver_unit_id": "id from input",
                    "importance": "central | supporting | minor",
                    "include_in_silver": True,
                    "rationale": "short reason grounded in the paper's main argument",
                }
            ]
        },
    }
    return [
        {
            "role": "system",
            "content": (
                "You assign importance tags to final canonical Silver claim units for a scientific paper. "
                "Use the paper title, abstract/summary, claim statements, evidence, and source context. "
                "central = core result or main argument; supporting = important supporting result, method, or qualification; "
                "minor = valid but peripheral, background, or low-value detail. "
                "Set include_in_silver=false for bibliographic, licensing, byline, document-structure, supplementary-inventory, "
                "or other trivial metadata statements that are not substantive scientific claims. Keep valid peripheral "
                "scientific findings with include_in_silver=true and importance=minor. "
                "Do not infer importance from miner/reference provenance. Return strict JSON only."
            ),
        },
        {"role": "user", "content": json.dumps(payload, sort_keys=True, ensure_ascii=False)},
    ]


def _parse_importance_response(content: str) -> dict[str, dict[str, Any]]:
    parsed = json.loads(_json_object_text(content))
    units = parsed.get("units") if isinstance(parsed, dict) else None
    if not isinstance(units, list):
        raise ValueError("importance response must include a units list")
    assignments: dict[str, dict[str, Any]] = {}
    for item in units:
        if not isinstance(item, dict):
            continue
        silver_unit_id = str(item.get("silver_unit_id") or "").strip()
        importance = str(item.get("importance") or "").strip().lower()
        include_in_silver = item.get("include_in_silver")
        if not silver_unit_id or importance not in IMPORTANCE_TAGS or not isinstance(include_in_silver, bool):
            continue
        assignments[silver_unit_id] = {
            "importance": importance,
            "include_in_silver": include_in_silver,
            "rationale": str(item.get("rationale") or "").strip(),
        }
    return assignments


def _json_object_text(content: str) -> str:
    text = content.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    if text.startswith("{"):
        return text
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        return text[start : end + 1]
    return text


def _direct_api_model_id(model: str, api_base: str) -> str:
    if "openrouter.ai/api" in api_base and model.startswith("openrouter/"):
        return model.removeprefix("openrouter/")
    return model


def silver_importance_classifier_from_env() -> OpenAICompatibleSilverImportanceClassifier | None:
    mode = os.getenv("CLAIMS_SILVER_IMPORTANCE_MODE", "openrouter").strip().lower()
    if mode in {"", "disabled", "none"}:
        return None
    api_key_env = os.getenv("CLAIMS_SILVER_IMPORTANCE_API_KEY_ENV", "OPENROUTER_API_KEY")
    api_base = os.getenv("CLAIMS_SILVER_IMPORTANCE_API_BASE", os.getenv("OPENROUTER_API_BASE", "https://openrouter.ai/api/v1"))
    return OpenAICompatibleSilverImportanceClassifier(
        model=os.getenv("CLAIMS_SILVER_IMPORTANCE_MODEL", "deepseek/deepseek-v4-flash"),
        api_key=os.getenv(api_key_env, ""),
        api_base=api_base,
        temperature=float(os.getenv("CLAIMS_SILVER_IMPORTANCE_TEMPERATURE", "0")),
        max_tokens=int(os.getenv("CLAIMS_SILVER_IMPORTANCE_MAX_TOKENS", "8192")),
        timeout_seconds=float(os.getenv("CLAIMS_SILVER_IMPORTANCE_TIMEOUT", "120")),
        batch_size=max(1, int(os.getenv("CLAIMS_SILVER_IMPORTANCE_BATCH_SIZE", "8"))),
    )


def _usage_from_response(response_payload: dict[str, Any]) -> dict[str, Any]:
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


def _empty_usage(source: str) -> dict[str, Any]:
    return {
        "prompt_tokens": None,
        "completion_tokens": None,
        "reasoning_tokens": None,
        "cache_read_tokens": None,
        "cache_write_tokens": None,
        "total_tokens": None,
        "cost_usd": None,
        "cost_kind": "unavailable",
        "source": source,
    }


def _merge_usage(total: dict[str, Any], addition: dict[str, Any]) -> dict[str, Any]:
    merged = dict(total)
    for key in (
        "prompt_tokens",
        "completion_tokens",
        "reasoning_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
        "total_tokens",
    ):
        values = [value for value in (total.get(key), addition.get(key)) if isinstance(value, int | float)]
        merged[key] = int(sum(values)) if values else None
    costs = [value for value in (total.get("cost_usd"), addition.get("cost_usd")) if isinstance(value, int | float)]
    merged["cost_usd"] = float(sum(costs)) if costs else None
    merged["cost_kind"] = "actual" if costs else "unavailable"
    merged["source"] = "aggregated_openai_compatible_responses"
    return merged


def _optional_int(value: Any) -> int | None:
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    return None
