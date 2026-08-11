from __future__ import annotations

import json
import hashlib
import os
import re
import shlex
import subprocess
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
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
PROMPT_DISPOSITIONS: set[str] = {
    "include_candidate",
    "exclude_candidate",
    "same_unit",
    "separate_valid_units",
    "candidate_a_only",
    "candidate_b_only",
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
        return vote_from_payload(
            context,
            payload,
            pass_id=self.pass_id,
            adjudication_profile_id=self.adjudication_profile_id,
            model_runtime_id=self.model_runtime_id,
        )


@dataclass(frozen=True)
class CLIAdjudicationPass:
    pass_id: str
    adjudication_profile_id: str
    model_runtime_id: str
    command: list[str] | str
    prompt_mode: str = "append"
    timeout_seconds: float = 900.0

    def run(self, context: AdjudicationContextBundle) -> AdjudicationVote:
        prompt = ""
        command: list[str] = []
        completed: subprocess.CompletedProcess[str] | None = None
        try:
            prompt = _adjudication_cli_prompt(context)
            command = shlex.split(self.command) if isinstance(self.command, str) else list(self.command)
            if not command:
                raise ValueError("CLI adjudication command is empty")
            stdin = None
            if self.prompt_mode == "stdin":
                stdin = prompt
            else:
                command.append(prompt)
            completed = subprocess.run(
                command,
                input=stdin,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds or None,
                check=False,
            )
            if completed.returncode != 0:
                raise RuntimeError(f"CLI exited {completed.returncode}: {completed.stderr[-1000:]}")
            payload = _parse_json_object(completed.stdout)
            _write_cli_debug_artifact(
                context=context,
                pass_id=self.pass_id,
                command=command,
                prompt=prompt,
                completed=completed,
            )
            return vote_from_payload(
                context,
                payload,
                pass_id=self.pass_id,
                adjudication_profile_id=self.adjudication_profile_id,
                model_runtime_id=self.model_runtime_id,
            )
        except Exception as exc:
            _write_cli_debug_artifact(
                context=context,
                pass_id=self.pass_id,
                command=command,
                prompt=prompt,
                completed=completed,
                error=f"{type(exc).__name__}: {exc}",
            )
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
                rationale=f"CLI adjudication pass failed: {type(exc).__name__}: {exc}",
                insufficient_information=True,
            )


def _adjudication_messages(context: AdjudicationContextBundle) -> list[dict[str, str]]:
    candidate_lines = _anonymous_candidate_payloads(context)
    user_payload = {
        "case": {
            "case_tracking_id": hashlib.sha256(context.case.case_id.encode("utf-8")).hexdigest()[:16],
            "paper_id": context.case.paper_id,
            "case_type": _neutral_case_type(context),
            "question": _neutral_question(context),
        },
        "candidates": candidate_lines,
        "source_context": context.source_context[:12000],
        "allowed_dispositions": sorted(PROMPT_DISPOSITIONS),
        "disposition_meanings": {
            "include_candidate": "For a single-candidate case, include the candidate in the final Silver record.",
            "exclude_candidate": "For a single-candidate case, reject the candidate.",
            "same_unit": "For a multi-candidate case, all candidates describe the same Silver unit.",
            "separate_valid_units": "For a multi-candidate case, candidates are valid but should remain separate Silver units.",
            "candidate_a_only": "For a two-candidate case, include candidate_1 and reject candidate_2.",
            "candidate_b_only": "For a two-candidate case, include candidate_2 and reject candidate_1.",
            "both_invalid": "Reject all candidates in this case.",
            "insufficient_information": "The supplied claim, evidence, and source context are insufficient to decide.",
        },
        "required_json_schema": {
            "disposition": "one allowed neutral disposition",
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
                "Resolve only the provided anonymous candidate case. "
                "Do not infer anything from candidate order. "
                "Use the claim statements, linked evidence records, source quotes, and source spans. "
                "Use the allowed disposition labels exactly. "
                "Cite source span ids when possible. "
                "Return only a JSON object matching the requested schema."
            ),
        },
        {"role": "user", "content": json.dumps(user_payload, sort_keys=True)},
    ]


def _adjudication_cli_prompt(context: AdjudicationContextBundle) -> str:
    messages = _adjudication_messages(context)
    return "\n\n".join(
        [
            "# Claims Silver adjudication task",
            "Resolve the provided anonymous scientific claim candidate case.",
            "Return only one strict JSON object. Do not wrap it in markdown.",
            "If you cannot decide from the supplied context, set disposition to `insufficient_information`.",
            "",
            "## Conversation Payload",
            json.dumps(messages, sort_keys=True),
            "",
            "## Required JSON",
            json.dumps(
                {
                    "disposition": "one allowed disposition",
                    "material_findings": ["short stable finding codes"],
                    "cited_span_ids": ["source span ids supporting the decision"],
                    "confidence": "number from 0 to 1",
                    "rationale": "brief explanation grounded in cited spans",
                    "insufficient_information": "boolean",
                },
                sort_keys=True,
            ),
        ]
    )


def vote_from_payload(
    context: AdjudicationContextBundle,
    payload: dict[str, Any],
    *,
    pass_id: str,
    adjudication_profile_id: str,
    model_runtime_id: str,
) -> AdjudicationVote:
    disposition, neutral_disposition = _coerce_prompt_disposition(context, payload.get("disposition"))
    cited_span_ids = _string_list(payload.get("cited_span_ids"))
    known_span_ids = {span_id for candidate in context.candidates for span_id in candidate.source_span_ids}
    valid_cited_span_ids = [span_id for span_id in cited_span_ids if not known_span_ids or span_id in known_span_ids]
    material_findings = _string_list(payload.get("material_findings")) or [disposition]
    if neutral_disposition and neutral_disposition != disposition:
        material_findings = [*material_findings, f"neutral_disposition:{neutral_disposition}"]
    confidence = _clamp_float(payload.get("confidence"), default=0.0)
    insufficient_information = bool(payload.get("insufficient_information")) or disposition == "insufficient_information"
    if insufficient_information:
        disposition = "insufficient_information"
        confidence = min(confidence, 0.5)

    return AdjudicationVote(
        case_id=context.case.case_id,
        pass_id=pass_id,
        adjudication_profile_id=adjudication_profile_id,
        model_runtime_id=model_runtime_id,
        candidate_order=[candidate.candidate_id for candidate in context.candidates],
        disposition=disposition,
        material_findings=material_findings,
        cited_span_ids=valid_cited_span_ids,
        confidence=confidence,
        rationale=str(payload.get("rationale") or ""),
        insufficient_information=insufficient_information,
    )


def _anonymous_candidate_payloads(context: AdjudicationContextBundle) -> list[dict[str, Any]]:
    payloads = []
    for index, candidate in enumerate(_anonymous_candidate_order(context), start=1):
        payloads.append(
            {
                "anonymous_id": f"candidate_{index}",
                "statement": candidate.statement,
                "qualifier": candidate.qualifier,
                "evidence_ids": candidate.evidence_ids,
                "source_span_ids": candidate.source_span_ids,
                "source_quotes": candidate.source_quotes[:3],
                "evidence_records": _candidate_evidence_records(candidate),
            }
        )
    return payloads


def _anonymous_candidate_order(context: AdjudicationContextBundle):
    seed = context.candidate_order_seed or context.case.case_id
    return sorted(
        context.candidates,
        key=lambda candidate: hashlib.sha256(f"{seed}|{candidate.candidate_id}".encode("utf-8")).hexdigest(),
    )


def _candidate_evidence_records(candidate: Any) -> list[dict[str, Any]]:
    records = candidate.metadata.get("evidence_records") if isinstance(candidate.metadata, dict) else None
    if not isinstance(records, list):
        return []
    cleaned = []
    for record in records[:5]:
        if not isinstance(record, dict):
            continue
        cleaned.append(
            {
                "evidence_id": record.get("evidence_id") or "",
                "title": record.get("title") or "",
                "role": record.get("role") or "",
                "summary": record.get("summary") or "",
                "evidence_method": record.get("evidence_method") or "",
                "outcome_type": record.get("outcome_type") or "",
                "presentation_type": record.get("presentation_type") or "",
                "source_refs": record.get("source_refs") if isinstance(record.get("source_refs"), list) else [],
            }
        )
    return cleaned


def _neutral_case_type(context: AdjudicationContextBundle) -> str:
    if len(context.candidates) <= 1:
        return "single_candidate"
    edge = context.case.metadata.get("candidate_graph_edge") if isinstance(context.case.metadata, dict) else None
    relation = edge.get("relation") if isinstance(edge, dict) else ""
    return f"candidate_relation:{relation or 'unknown'}"


def _neutral_question(context: AdjudicationContextBundle) -> str:
    if len(context.candidates) <= 1:
        return "Should this candidate be included in the final Silver record?"
    return "Do these candidates represent the same Silver unit, separate valid units, or should any candidate be rejected?"


def _coerce_prompt_disposition(
    context: AdjudicationContextBundle,
    value: Any,
) -> tuple[AdjudicationDisposition, str]:
    normalized = str(value or "").strip().lower()
    if normalized in ALLOWED_DISPOSITIONS:
        return normalized, normalized  # type: ignore[return-value]
    if normalized not in PROMPT_DISPOSITIONS:
        return "insufficient_information", normalized
    if normalized == "insufficient_information":
        return "insufficient_information", normalized
    if normalized == "both_invalid":
        return "both_invalid", normalized
    if normalized == "same_unit":
        return "benign_difference", normalized
    if normalized == "separate_valid_units":
        return "both_valid", normalized
    ordered = _anonymous_candidate_order(context)
    if normalized == "include_candidate":
        return "accepted_improvement", normalized
    if normalized == "exclude_candidate":
        candidate = ordered[0] if ordered else None
        return ("reference_error" if getattr(candidate, "origin", "") == "bronze" else "miner_error"), normalized
    if normalized in {"candidate_a_only", "candidate_b_only"}:
        accepted_index = 0 if normalized == "candidate_a_only" else 1
        accepted = ordered[accepted_index] if len(ordered) > accepted_index else None
        rejected = [candidate for index, candidate in enumerate(ordered) if index != accepted_index]
        if getattr(accepted, "origin", "") == "bronze" and any(candidate.origin == "miner" for candidate in rejected):
            return "miner_error", normalized
        if getattr(accepted, "origin", "") == "miner" and any(candidate.origin == "bronze" for candidate in rejected):
            return "reference_error", normalized
        return "both_valid", normalized
    return "insufficient_information", normalized


def _parse_json_object(content: str) -> dict[str, Any]:
    stripped = content.strip()
    candidates = [stripped]
    candidates.extend(match.group(1) for match in re.finditer(r"```(?:json)?\s*(\{.*?\})\s*```", stripped, flags=re.DOTALL))
    candidates.extend(_json_object_candidates(stripped))
    last_error: Exception | None = None
    for candidate in reversed(candidates):
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError as exc:
            last_error = exc
            try:
                parsed = json.loads(_escape_control_chars_in_json_strings(candidate))
            except json.JSONDecodeError as repaired_exc:
                last_error = repaired_exc
                continue
        if not isinstance(parsed, dict):
            raise ValueError("adjudication pass returned non-object JSON")
        return parsed
    if last_error is not None:
        raise last_error
    raise ValueError("adjudication pass did not return a JSON object")


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


def _escape_control_chars_in_json_strings(text: str) -> str:
    output: list[str] = []
    in_string = False
    escaped = False
    for char in text:
        if in_string:
            if escaped:
                output.append(char)
                escaped = False
                continue
            if char == "\\":
                output.append(char)
                escaped = True
                continue
            if char == '"':
                output.append(char)
                in_string = False
                continue
            if char == "\n":
                output.append("\\n")
                continue
            if char == "\r":
                output.append("\\r")
                continue
            if char == "\t":
                output.append("\\t")
                continue
        else:
            if char == '"':
                in_string = True
        output.append(char)
    return "".join(output)


def _write_cli_debug_artifact(
    *,
    context: AdjudicationContextBundle,
    pass_id: str,
    command: list[str],
    prompt: str,
    completed: subprocess.CompletedProcess[str] | None,
    error: str | None = None,
) -> None:
    debug_root = os.getenv("CLAIMS_SILVER_ADJUDICATION_DEBUG_DIR", "").strip()
    if not debug_root:
        return
    root = Path(debug_root).expanduser()
    root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
    path = root / f"{stamp}_{context.case.case_id}_{pass_id}.json"
    payload = {
        "case_id": context.case.case_id,
        "pass_id": pass_id,
        "command": command,
        "prompt_mode": "debug_capture",
        "prompt_chars": len(prompt),
        "prompt": prompt if os.getenv("CLAIMS_SILVER_ADJUDICATION_DEBUG_PROMPT", "").strip().lower() in {"1", "true", "yes"} else "",
        "returncode": completed.returncode if completed is not None else None,
        "stdout": completed.stdout if completed is not None else "",
        "stderr": completed.stderr if completed is not None else "",
        "error": error,
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


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
