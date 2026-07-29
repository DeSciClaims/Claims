from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .adjudication_models import AdjudicationContextBundle, AdjudicationDisposition, AdjudicationVote


ALLOWED_DISPOSITIONS: set[str] = {
    "miner_error",
    "reference_error",
    "accepted_improvement",
    "benign_difference",
    "both_valid",
    "both_invalid",
    "insufficient_information",
}


@dataclass(frozen=True)
class StaticAdjudicationPass:
    pass_id: str
    adjudication_profile_id: str
    model_runtime_id: str
    dispositions_by_case_id: dict[str, AdjudicationDisposition]
    default_disposition: AdjudicationDisposition = "insufficient_information"
    confidence: float = 0.95

    def run(self, context: AdjudicationContextBundle) -> AdjudicationVote:
        disposition = self.dispositions_by_case_id.get(context.case.case_id, self.default_disposition)
        return AdjudicationVote(
            case_id=context.case.case_id,
            pass_id=self.pass_id,
            adjudication_profile_id=self.adjudication_profile_id,
            model_runtime_id=self.model_runtime_id,
            candidate_order=[candidate.candidate_id for candidate in context.candidates],
            disposition=disposition,
            material_findings=[disposition],
            cited_span_ids=sorted({span_id for candidate in context.candidates for span_id in candidate.source_span_ids}),
            confidence=self.confidence,
            rationale=f"Static adjudication pass selected {disposition}.",
            insufficient_information=disposition == "insufficient_information",
        )


@dataclass(frozen=True)
class OpenAICompatibleAdjudicationPass:
    pass_id: str
    adjudication_profile_id: str
    model_runtime_id: str
    model: str
    api_key: str
    api_base: str = "https://api.openai.com/v1"
    temperature: float = 0.0
    max_tokens: int = 2048
    timeout_seconds: float = 90.0
    completion_fn: Callable[[list[dict[str, str]]], str] | None = field(default=None, compare=False, repr=False)

    def run(self, context: AdjudicationContextBundle) -> AdjudicationVote:
        messages = _adjudication_messages(context)
        try:
            content = self.completion_fn(messages) if self.completion_fn is not None else self._complete(messages)
            payload = _parse_json_object(content)
            return self._vote_from_payload(context, payload)
        except Exception as exc:
            return AdjudicationVote(
                case_id=context.case.case_id,
                pass_id=self.pass_id,
                adjudication_profile_id=self.adjudication_profile_id,
                model_runtime_id=self.model_runtime_id,
                candidate_order=[candidate.candidate_id for candidate in context.candidates],
                disposition="insufficient_information",
                material_findings=["adjudication_pass_failed"],
                cited_span_ids=[],
                confidence=0.0,
                rationale=f"Adjudication pass failed: {type(exc).__name__}: {exc}",
                insufficient_information=True,
            )

    def _complete(self, messages: list[dict[str, str]]) -> str:
        endpoint = f"{self.api_base.rstrip('/')}/chat/completions"
        request_body = json.dumps(
            {
                "model": self.model,
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
            raise RuntimeError(f"model endpoint returned HTTP {exc.code}: {body[:500]}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"model endpoint unavailable: {exc.reason}") from exc

        choices = response_payload.get("choices") if isinstance(response_payload, dict) else None
        if not choices:
            raise ValueError("model endpoint response did not include choices")
        message = choices[0].get("message") if isinstance(choices[0], dict) else None
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, str) or not content.strip():
            raise ValueError("model endpoint response did not include message content")
        return content

    def _vote_from_payload(self, context: AdjudicationContextBundle, payload: dict[str, Any]) -> AdjudicationVote:
        disposition = _coerce_disposition(payload.get("disposition"))
        cited_span_ids = _string_list(payload.get("cited_span_ids"))
        known_span_ids = {span_id for candidate in context.candidates for span_id in candidate.source_span_ids}
        valid_cited_span_ids = [span_id for span_id in cited_span_ids if not known_span_ids or span_id in known_span_ids]
        material_findings = _string_list(payload.get("material_findings")) or [disposition]
        confidence = _clamp_float(payload.get("confidence"), default=0.0)
        insufficient_information = bool(payload.get("insufficient_information")) or disposition == "insufficient_information"
        if insufficient_information:
            disposition = "insufficient_information"
            confidence = min(confidence, 0.5)

        return AdjudicationVote(
            case_id=context.case.case_id,
            pass_id=self.pass_id,
            adjudication_profile_id=self.adjudication_profile_id,
            model_runtime_id=self.model_runtime_id,
            candidate_order=[candidate.candidate_id for candidate in context.candidates],
            disposition=disposition,
            material_findings=material_findings,
            cited_span_ids=valid_cited_span_ids,
            confidence=confidence,
            rationale=str(payload.get("rationale") or ""),
            insufficient_information=insufficient_information,
        )


def _adjudication_messages(context: AdjudicationContextBundle) -> list[dict[str, str]]:
    candidate_lines = []
    for candidate in context.candidates:
        candidate_lines.append(
            {
                "candidate_id": candidate.candidate_id,
                "origin": candidate.origin,
                "miner_id": candidate.miner_id,
                "record_id": candidate.record_id,
                "statement": candidate.statement,
                "source_span_ids": candidate.source_span_ids,
                "source_quotes": candidate.source_quotes[:3],
            }
        )
    user_payload = {
        "case": context.case.model_dump(mode="json"),
        "candidate_order_seed": context.candidate_order_seed,
        "candidates": candidate_lines,
        "source_context": context.source_context[:12000],
        "allowed_dispositions": sorted(ALLOWED_DISPOSITIONS),
        "required_json_schema": {
            "disposition": "one allowed disposition",
            "material_findings": ["short stable finding codes"],
            "cited_span_ids": ["source span ids supporting the decision"],
            "confidence": "number from 0 to 1",
            "rationale": "brief explanation grounded in cited spans",
            "insufficient_information": "boolean",
        },
    }
    return [
        {
            "role": "system",
            "content": (
                "You are an adjudication pass for scientific claim extraction. "
                "Resolve only the provided Bronze/miner discrepancy. "
                "Use the allowed disposition labels exactly. "
                "Cite source span ids when possible. "
                "Return only a JSON object matching the requested schema."
            ),
        },
        {"role": "user", "content": json.dumps(user_payload, sort_keys=True)},
    ]


def _parse_json_object(content: str) -> dict[str, Any]:
    stripped = content.strip()
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", stripped, flags=re.DOTALL)
        if match is None:
            match = re.search(r"(\{.*\})", stripped, flags=re.DOTALL)
        if match is None:
            raise
        parsed = json.loads(match.group(1))
    if not isinstance(parsed, dict):
        raise ValueError("adjudication pass returned non-object JSON")
    return parsed


def _coerce_disposition(value: Any) -> AdjudicationDisposition:
    disposition = str(value or "").strip().lower()
    if disposition not in ALLOWED_DISPOSITIONS:
        return "insufficient_information"
    return disposition  # type: ignore[return-value]


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _clamp_float(value: Any, *, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return max(0.0, min(1.0, number))
