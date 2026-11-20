"""Summarize the append-only inference ledger without losing resumed attempts."""
from __future__ import annotations

from collections import Counter
import json
from pathlib import Path


def ledger_events(directory: Path) -> list[dict]:
    path = directory / "inference_calls.jsonl"
    if not path.exists():
        return []
    events = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            events.append(json.loads(line))
    return events


def summarize_calls(directory: Path) -> dict:
    events = ledger_events(directory)
    starts = {event["call_id"]: event for event in events if event["event"] == "request_start"}
    outcomes = {event["call_id"]: event for event in events if event["event"] in {"request_complete", "request_error"}}
    completed = [event for event in outcomes.values() if event["event"] == "request_complete"]
    errors = [event for event in outcomes.values() if event["event"] == "request_error"]
    known = [event for event in completed if event.get("usage", {}).get("cost") is not None]
    provider = [event for event in known if event["cost_source"] == "provider_usage"]
    estimated = [event for event in known if event["cost_source"] != "provider_usage"]
    return {
        "started_requests": len(starts), "completed_requests": len(completed),
        "failed_requests": len(errors), "unfinished_requests": len(set(starts) - set(outcomes)),
        "program_invocations": len({event["program_id"] for event in starts.values()}),
        "rate_limit_errors": sum(event.get("status_code") == 429 for event in errors),
        "truncated_completions": sum(event.get("finish_reason") == "length" for event in completed),
        "provider_reported_cost_usd": sum(event["usage"]["cost"] for event in provider),
        "litellm_response_cost_usd": sum(event["usage"]["cost"] for event in estimated),
        "recorded_cost_usd": sum(event["usage"]["cost"] for event in known),
        "requests_without_cost": len(starts) - len(known),
        "input_tokens": sum(event["usage"].get("prompt_tokens", 0) or 0 for event in completed),
        "output_tokens": sum(event["usage"].get("completion_tokens", 0) or 0 for event in completed),
        "reasoning_tokens": sum((event["usage"].get("completion_tokens_details") or {}).get("reasoning_tokens", 0) or 0 for event in completed),
        "requests_by_stage": dict(Counter(
            next((name for name in ("source-table-audit", "graph-review", "inventory", "results", "families", "repair")
                  if event["stage"] == name or event["stage"].startswith(name + "-")), event["stage"])
            for event in starts.values())),
        "cost_note": "Provider usage cost is authoritative when available. LiteLLM response_cost is reported separately. Interrupted/errors without usage have unknown billing.",
    }
