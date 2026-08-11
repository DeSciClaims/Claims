from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from .comparison_models import SilverRecord


IMPORTANCE_TAGS = {"central", "supporting", "minor"}


class SilverImportanceClassifier(Protocol):
    def classify(
        self,
        silver_record: SilverRecord,
        *,
        paper_context: dict[str, Any],
        source_context: str,
    ) -> dict[str, dict[str, str]]:
        """Return silver_unit_id -> {"importance": tag, "rationale": text}."""


@dataclass(frozen=True)
class OpenAICompatibleSilverImportanceClassifier:
    model: str
    api_key: str
    api_base: str = "https://openrouter.ai/api/v1"
    temperature: float = 0.0
    max_tokens: int = 2048
    timeout_seconds: float = 120.0
    completion_fn: Callable[[list[dict[str, str]]], str] | None = field(default=None, compare=False, repr=False)

    def classify(
        self,
        silver_record: SilverRecord,
        *,
        paper_context: dict[str, Any],
        source_context: str,
    ) -> dict[str, dict[str, str]]:
        if not silver_record.silver_units:
            return {}
        messages = _importance_messages(
            silver_record=silver_record,
            paper_context=paper_context,
            source_context=source_context,
        )
        content = self.completion_fn(messages) if self.completion_fn is not None else self._complete(messages)
        return _parse_importance_response(content)

    def _complete(self, messages: list[dict[str, str]]) -> str:
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
        return content


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
            "note": "Silver unit tags retain their default values.",
        }
        return silver_record

    applied: dict[str, dict[str, str]] = {}
    for unit in silver_record.silver_units:
        assignment = assignments.get(unit.silver_unit_id) or {}
        importance = str(assignment.get("importance") or "").strip().lower()
        if importance not in IMPORTANCE_TAGS:
            continue
        unit.importance = importance  # type: ignore[assignment]
        rationale = str(assignment.get("rationale") or "").strip()
        unit.metadata["importance_assignment"] = {
            "importance": importance,
            "rationale": rationale,
            "source": "validator_silver_importance_classifier",
        }
        applied[unit.silver_unit_id] = {"importance": importance, "rationale": rationale}
    silver_record.metadata["importance_assignment"] = {
        "mode": "classifier",
        "applied_count": len(applied),
        "unit_count": len(silver_record.silver_units),
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
                "Do not infer importance from miner/reference provenance. Return strict JSON only."
            ),
        },
        {"role": "user", "content": json.dumps(payload, sort_keys=True, ensure_ascii=False)},
    ]


def _parse_importance_response(content: str) -> dict[str, dict[str, str]]:
    parsed = json.loads(_json_object_text(content))
    units = parsed.get("units") if isinstance(parsed, dict) else None
    if not isinstance(units, list):
        raise ValueError("importance response must include a units list")
    assignments: dict[str, dict[str, str]] = {}
    for item in units:
        if not isinstance(item, dict):
            continue
        silver_unit_id = str(item.get("silver_unit_id") or "").strip()
        importance = str(item.get("importance") or "").strip().lower()
        if not silver_unit_id or importance not in IMPORTANCE_TAGS:
            continue
        assignments[silver_unit_id] = {
            "importance": importance,
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
        max_tokens=int(os.getenv("CLAIMS_SILVER_IMPORTANCE_MAX_TOKENS", "2048")),
        timeout_seconds=float(os.getenv("CLAIMS_SILVER_IMPORTANCE_TIMEOUT", "120")),
    )
