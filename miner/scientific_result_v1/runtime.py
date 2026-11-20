"""DSPy stage execution with explicit retries and an append-only inference ledger."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
from pathlib import Path
import time
from typing import Any
import uuid

import dspy
from dspy.utils.exceptions import AdapterParseError

from .programs import JSONStageAdapter, stage_program


class TruncatedStageError(ValueError):
    """A complete response cannot be accepted when the provider reports length."""


def as_dict(value: Any) -> dict:
    if isinstance(value, dict):
        return value
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    return dict(value)


@dataclass
class DspyRuntime:
    model: str
    api_key: str = field(repr=False)
    api_base: str = "https://openrouter.ai/api/v1"
    model_route: str | None = None
    max_tokens: int = 32768
    temperature: float = 0.0
    reasoning_effort: str = "low"
    retry_reasoning_effort: str | None = None
    timeout: int = 600
    request_deadline: float | None = None
    transport_retries: int = 2
    usage: list[dict] = field(default_factory=list)
    runtime_name: str = field(default="dspy-predict", init=False)
    runtime_version: str = field(default="2", init=False)

    @property
    def api_model(self) -> str:
        return self.model_route or self.model.removeprefix("openrouter/")

    def _append(self, output_dir: Path, event: dict) -> None:
        # One paper directory has one sequential runtime. UUID filenames preserve
        # every completed/failed call even when stage labels are reused on resume.
        output_dir.mkdir(parents=True, exist_ok=True)
        event = {"timestamp": datetime.now(timezone.utc).isoformat(), **event}
        with (output_dir.parent / "inference_calls.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")
            handle.flush()

    def _record_response(self, *, response: Any, output_dir: Path, metadata: dict, started: float) -> None:
        raw = as_dict(response)
        usage = as_dict(raw.get("usage") or {})
        hidden = getattr(response, "_hidden_params", {}) or {}
        cost = usage.get("cost")
        cost_source = "provider_usage" if cost is not None else "unavailable"
        if cost is None and hidden.get("response_cost") is not None:
            cost = hidden["response_cost"]
            cost_source = "litellm_response_cost"
        if cost is not None:
            usage["cost"] = cost
        choices = raw.get("choices") or []
        finish_reason = choices[0].get("finish_reason") if choices else None
        text = (choices[0].get("message") or {}).get("content") or "" if choices else ""
        record = {**metadata, "event": "request_complete", "usage": usage,
                  "cost_source": cost_source, "provider_request_id": raw.get("id"),
                  "finish_reason": finish_reason, "provider": raw.get("provider"),
                  "provider_model": raw.get("model"), "elapsed_seconds": round(time.monotonic() - started, 3)}
        self.usage.append(record)
        self._append(output_dir, record)
        call_id = metadata["call_id"]
        (output_dir / f"{call_id}.usage.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
        (output_dir / f"{call_id}.response.txt").write_text(text, encoding="utf-8")
        if finish_reason == "length":
            raise TruncatedStageError(f'{metadata["stage"]}: provider truncated output at {self.max_tokens} tokens')

    def effective_reasoning(self, stage: str, payload: dict) -> str:
        if self.reasoning_effort == "none" and self.retry_reasoning_effort and (
            payload.get("validation_feedback") or stage.startswith(("repair", "graph-review", "source-table-audit"))
        ):
            return self.retry_reasoning_effort
        return self.reasoning_effort

    def _lm(self, *, stage: str, output_dir: Path, program_id: str, reasoning_effort: str | None = None):
        runtime = self
        effective_reasoning = reasoning_effort or self.reasoning_effort

        class RecordedLM(dspy.LM):
            def forward(self, prompt=None, messages=None, **kwargs):
                for attempt in range(runtime.transport_retries + 1):
                    started = time.monotonic()
                    metadata = {"call_id": str(uuid.uuid4()), "program_id": program_id,
                                "stage": stage, "runtime": runtime.runtime_name,
                                "model": runtime.model, "api_model": runtime.api_model,
                                "reasoning_effort": effective_reasoning,
                                "transport_attempt": attempt + 1, "request_deadline_seconds": runtime.request_deadline}
                    runtime._append(output_dir, {**metadata, "event": "request_start"})
                    # Auditable model-facing messages, without credentials.
                    (output_dir / f'{metadata["call_id"]}.request.json').write_text(
                        json.dumps({"messages": messages, "model": runtime.api_model,
                                    "temperature": runtime.temperature, "max_tokens": runtime.max_tokens,
                                    "reasoning": {"effort": effective_reasoning, "exclude": True},
                                    "response_format": kwargs.get("response_format")},
                                   ensure_ascii=False, indent=2), encoding="utf-8")
                    try:
                        if runtime.request_deadline is None:
                            response = super().forward(prompt=prompt, messages=messages, **kwargs)
                        else:
                            from .deadline import run_bounded
                            response = run_bounded(
                                lambda: super(RecordedLM, self).aforward(prompt=prompt, messages=messages, **kwargs),
                                runtime.request_deadline)
                    except Exception as exc:
                        status = getattr(exc, "status_code", None)
                        retryable = status in {408, 429, 500, 502, 503, 504} or type(exc).__name__ in {"Timeout", "TimeoutError", "APIConnectionError", "ServiceUnavailableError", "InternalServerError"}
                        record = {**metadata, "event": "request_error", "status_code": status,
                                  "error_type": type(exc).__name__, "retryable": retryable,
                                  "usage": {}, "cost_source": "unavailable",
                                  "elapsed_seconds": round(time.monotonic() - started, 3)}
                        runtime.usage.append(record)
                        runtime._append(output_dir, record)
                        (output_dir / f'{metadata["call_id"]}.error.json').write_text(json.dumps(record, indent=2), encoding="utf-8")
                        if not retryable or attempt >= runtime.transport_retries:
                            raise
                        headers = getattr(getattr(exc, "response", None), "headers", {}) or {}
                        try:
                            wait = max(2 ** attempt, float(headers.get("retry-after", 0)))
                        except (TypeError, ValueError):
                            wait = 2 ** attempt
                        # Do not retry before a provider's Retry-After deadline.
                        # Long deferrals end this bounded run instead of hammering it.
                        if wait > 60:
                            raise
                        time.sleep(wait)
                        continue
                    runtime._record_response(response=response, output_dir=output_dir,
                                             metadata=metadata, started=started)
                    return response
                raise RuntimeError("Unreachable retry state")

        # DSPy/LiteLLM consumes the first provider prefix. A scoped gateway keeps
        # an additional openrouter/ route in api_model, as in the existing miner.
        return RecordedLM(
            model="openrouter/" + self.api_model, model_type="chat",
            api_key=self.api_key, api_base=self.api_base, temperature=self.temperature,
            max_tokens=self.max_tokens, timeout=self.timeout,
            reasoning={"effort": effective_reasoning, "exclude": True},
            cache=False, num_retries=0,
        )

    def generate(self, *, instructions: str, payload: dict, output_dir: Path, stage: str) -> dict:
        images = payload.get("source_images", [])
        if images and self.model.removeprefix("openrouter/") == "deepseek/deepseek-v4-flash-0731":
            raise ValueError("DeepSeek V4 Flash 0731 accepts text only; disable vision or select a vision-capable Qwen model")
        output_dir.mkdir(parents=True, exist_ok=True)
        program_id = str(uuid.uuid4())
        program = stage_program(stage, schema=payload["stage_schema"], instructions=instructions, vision=bool(images))
        lm = self._lm(stage=stage, output_dir=output_dir, program_id=program_id,
                      reasoning_effort=self.effective_reasoning(stage, payload))
        try:
            with dspy.context(lm=lm, adapter=JSONStageAdapter()):
                prediction = program(stage_request=payload)
                result = prediction.toDict()
        except AdapterParseError as exc:
            # Provider completion has already been durably logged by RecordedLM.
            raise ValueError(f"{stage}: DSPy could not parse the declared stage outputs ({type(exc).__name__})") from exc
        path = output_dir / f"{stage}.{program_id}.prediction.json"
        path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        (output_dir / f"{stage}.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        return result
