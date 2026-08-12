from __future__ import annotations

import threading
import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any


UsageSink = Callable[[dict[str, Any]], None]


class ModelUsageCollector:
    def __init__(self, *, network: str, run_id: str, batch_id: str) -> None:
        self.network = network
        self.run_id = run_id
        self.batch_id = batch_id
        self._events: list[dict[str, Any]] = []
        self._lock = threading.Lock()

    def record(self, event: dict[str, Any]) -> None:
        usage = event.get("usage") if isinstance(event.get("usage"), dict) else {}
        cost_usd = _optional_float(usage.get("cost_usd"))
        cost_kind = str(usage.get("cost_kind") or "unavailable")
        if cost_kind not in {"actual", "estimated"} or cost_usd is None:
            cost_kind = "unavailable"
        row = {
            "usage_event_id": str(event.get("usage_event_id") or f"usage_{uuid.uuid4().hex}"),
            "network": self.network,
            "run_id": self.run_id,
            "batch_id": self.batch_id,
            "paper_id": _optional_text(event.get("paper_id")),
            "uid": _optional_int(event.get("uid")),
            "stage_key": str(event.get("stage_key") or "unknown"),
            "stage_label": str(event.get("stage_label") or event.get("stage_key") or "Unknown"),
            "role": str(event.get("role") or "validator"),
            "operation_id": _optional_text(event.get("operation_id")),
            "pass_id": _optional_text(event.get("pass_id")),
            "case_id": _optional_text(event.get("case_id")),
            "harness": str(event.get("harness") or "unknown"),
            "runtime": str(event.get("runtime") or event.get("harness") or "unknown"),
            "provider": str(event.get("provider") or "unknown"),
            "model": str(event.get("model") or "unknown"),
            "prompt_tokens": _optional_int(usage.get("prompt_tokens")),
            "completion_tokens": _optional_int(usage.get("completion_tokens")),
            "reasoning_tokens": _optional_int(usage.get("reasoning_tokens")),
            "cache_read_tokens": _optional_int(usage.get("cache_read_tokens")),
            "cache_write_tokens": _optional_int(usage.get("cache_write_tokens")),
            "total_tokens": _optional_int(usage.get("total_tokens")),
            "cost_usd": cost_usd,
            "cost_kind": cost_kind,
            "usage_source": str(usage.get("source") or event.get("usage_source") or "unavailable"),
            "status": str(event.get("status") or "success"),
            "error": _optional_text(event.get("error")),
            "started_at": _timestamp(event.get("started_at")),
            "ended_at": _timestamp(event.get("ended_at")),
            "duration_seconds": _optional_float(event.get("duration_seconds")),
            "metadata": event.get("metadata") if isinstance(event.get("metadata"), dict) else {},
        }
        with self._lock:
            self._events.append(row)

    def snapshot(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(event) for event in self._events]


def provider_from_model_or_base(model: str, api_base: str = "") -> str:
    if "openrouter.ai" in api_base or model.startswith("openrouter/") or "/" in model:
        return "openrouter"
    if "api.openai.com" in api_base or model.startswith(("gpt-", "o1", "o3", "o4")):
        return "openai"
    return "unknown"


def _optional_text(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _optional_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _optional_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return round(float(value), 8)
    except (TypeError, ValueError):
        return None


def _timestamp(value: Any) -> str:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    text = str(value or "").strip()
    return text or datetime.now(timezone.utc).isoformat()
