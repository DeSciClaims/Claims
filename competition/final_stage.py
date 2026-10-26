"""Identify the evaluated final stage after a possible reference fallback."""

from __future__ import annotations

from typing import Any, Mapping


def effective_final_stage(ledger: list[Mapping[str, Any]]) -> str:
    if not ledger:
        raise ValueError("final stage has not started")
    last = ledger[-1]
    stage = last.get("stage")
    if stage in {"final", "week_one"}:
        return str(stage)
    if stage == "rerun" and last.get("source_stage") == "final":
        return "rerun"
    raise ValueError("final stage has not completed")
