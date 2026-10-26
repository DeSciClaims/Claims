"""Capture trusted judge usage for one frozen paper evaluation."""

from __future__ import annotations

import math
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Iterator


class JudgeUsageCapture:
    def __init__(self) -> None:
        self._current: ContextVar[tuple[str, list[dict[str, Any]]] | None] = ContextVar(
            "competition_judge_usage", default=None,
        )

    @contextmanager
    def collect(self, paper_id: str) -> Iterator[list[dict[str, Any]]]:
        events: list[dict[str, Any]] = []
        token = self._current.set((paper_id, events))
        try:
            yield events
        finally:
            self._current.reset(token)

    def record(self, event: dict[str, Any]) -> None:
        current = self._current.get()
        if current is None:
            return  # Gold and other calls need their own durable accounting path.
        paper_id, events = current
        if event.get("paper_id") != paper_id:
            raise ValueError("judge usage event belongs to another paper")
        usage = event.get("usage")
        if not isinstance(usage, dict):
            usage = {}
        amount = usage.get("cost_usd")
        kind = usage.get("cost_kind")
        if (kind not in {"actual", "estimated"}
            or isinstance(amount, bool) or not isinstance(amount, (int, float))
            or not math.isfinite(amount) or amount < 0):
            kind, amount = "unavailable", None
        events.append({
            "model": str(event.get("model") or "unknown"),
            "pass_id": str(event.get("pass_id") or "unknown"),
            "status": str(event.get("status") or "unknown"),
            "cost_kind": kind, "cost_usd": amount,
        })
