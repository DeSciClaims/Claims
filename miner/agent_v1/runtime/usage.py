from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any


def empty_usage(source: str) -> dict[str, Any]:
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


def merge_usage(usages: list[dict[str, Any]]) -> dict[str, Any]:
    merged = empty_usage("unavailable")
    sources: list[str] = []
    for usage in usages:
        if not usage:
            continue
        source = usage.get("source")
        if source and source not in sources:
            sources.append(str(source))
        for key in (
            "prompt_tokens",
            "completion_tokens",
            "reasoning_tokens",
            "cache_read_tokens",
            "cache_write_tokens",
            "total_tokens",
        ):
            value = usage.get(key)
            if isinstance(value, int):
                merged[key] = (merged[key] or 0) + value
        cost = usage.get("cost_usd")
        if isinstance(cost, int | float):
            merged["cost_usd"] = round((merged["cost_usd"] or 0.0) + float(cost), 8)
    cost_kinds = {
        str(usage.get("cost_kind"))
        for usage in usages
        if usage and usage.get("cost_usd") is not None and usage.get("cost_kind") in {"actual", "estimated"}
    }
    if cost_kinds:
        merged["cost_kind"] = "actual" if cost_kinds == {"actual"} else "estimated"
    merged["source"] = ",".join(sources) if sources else "unavailable"
    return merged


def usage_from_dspy_lm(lm: Any) -> dict[str, Any]:
    history = getattr(lm, "history", None) or []
    return _usage_from_records(history, source="dspy_lm_history")


def usage_from_langchain_result(result: Any) -> dict[str, Any]:
    messages = result.get("messages", []) if isinstance(result, dict) else []
    usages: list[dict[str, Any]] = []
    for message in messages:
        usage = getattr(message, "usage_metadata", None)
        if isinstance(usage, dict):
            usages.append(_normalize_usage_dict(usage, source="langchain_usage_metadata"))
            continue
        response_metadata = getattr(message, "response_metadata", None)
        if isinstance(response_metadata, dict):
            token_usage = response_metadata.get("token_usage")
            if isinstance(token_usage, dict):
                usages.append(_normalize_usage_dict(token_usage, source="langchain_response_metadata"))
    return merge_usage(usages)


def usage_from_cli_process(command: list[str], stdout: str, stderr: str) -> dict[str, Any]:
    hermes_machine_usage = usage_from_hermes_machine_output(stderr)
    if _has_usage(hermes_machine_usage):
        return hermes_machine_usage

    codex_usage = usage_from_codex_jsonl(stdout)
    if _has_usage(codex_usage):
        return codex_usage

    hermes_usage = usage_from_hermes_stdout(stdout, command_prefix=_hermes_command_prefix(command))
    if _has_usage(hermes_usage):
        return hermes_usage

    executable = Path(command[0]).name if command else ""
    if executable == "codex":
        return codex_usage
    if executable == "hermes":
        return hermes_usage
    for item in command:
        name = Path(item).name
        if name == "codex":
            return codex_usage
        if name == "hermes":
            return hermes_usage
    return empty_usage("cli_unavailable")


def usage_from_hermes_machine_output(stderr: str) -> dict[str, Any]:
    prefix = "CLAIMS_HERMES_USAGE_JSON="
    for line in reversed(stderr.splitlines()):
        if not line.startswith(prefix):
            continue
        try:
            payload = json.loads(line[len(prefix) :])
        except Exception:
            return empty_usage("hermes_machine_usage_invalid")
        if not isinstance(payload, dict):
            return empty_usage("hermes_machine_usage_invalid")
        usage = _normalize_usage_dict(payload, source="hermes_oneshot")
        actual_cost = _float_value(payload, "actual_cost_usd")
        estimated_cost = _float_value(payload, "estimated_cost_usd")
        reported_cost = _float_value(payload, "cost_usd")
        reported_cost_kind = str(payload.get("cost_kind") or "").strip().lower()
        if actual_cost is not None:
            usage["cost_usd"] = actual_cost
            usage["cost_kind"] = "actual"
        elif estimated_cost is not None:
            usage["cost_usd"] = estimated_cost
            usage["cost_kind"] = "estimated"
        elif reported_cost is not None and reported_cost_kind in {"actual", "estimated"}:
            usage["cost_usd"] = reported_cost
            usage["cost_kind"] = reported_cost_kind
        return usage
    return empty_usage("hermes_machine_usage_not_found")


def usage_from_codex_jsonl(stdout: str) -> dict[str, Any]:
    usages: list[dict[str, Any]] = []
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except Exception:
            continue
        if not isinstance(event, dict) or event.get("type") != "turn.completed":
            continue
        usage = event.get("usage")
        if isinstance(usage, dict):
            prompt_tokens = _int_value(usage, "input_tokens", "prompt_tokens")
            completion_tokens = _int_value(usage, "output_tokens", "completion_tokens")
            reasoning_tokens = _int_value(usage, "reasoning_output_tokens") or 0
            total_tokens = None
            if prompt_tokens is not None and completion_tokens is not None:
                total_tokens = prompt_tokens + completion_tokens + reasoning_tokens
            usages.append(
                {
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "reasoning_tokens": reasoning_tokens or None,
                    "cache_read_tokens": _int_value(usage, "cached_input_tokens", "cache_read_tokens"),
                    "cache_write_tokens": _int_value(usage, "cache_write_tokens"),
                    "total_tokens": total_tokens,
                    "cost_usd": _float_value(usage, "cost_usd", "cost"),
                    "cost_kind": "estimated" if _float_value(usage, "cost_usd", "cost") is not None else "unavailable",
                    "source": "codex_exec_json",
                }
            )
    return merge_usage(usages)


def usage_from_hermes_stdout(stdout: str, *, command_prefix: list[str] | None = None) -> dict[str, Any]:
    session_id = _hermes_session_id(stdout)
    if not session_id:
        return empty_usage("hermes_session_not_found")
    prefix = command_prefix or ([hermes] if (hermes := shutil.which("hermes")) else [])
    if not prefix:
        return empty_usage("hermes_cli_not_found")
    try:
        completed = subprocess.run(
            [
                *prefix,
                "sessions",
                "export",
                "-",
                "--format",
                "jsonl",
                "--session-id",
                session_id,
                "--redact",
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except Exception:
        return empty_usage("hermes_sessions_export_failed")
    if completed.returncode != 0:
        return empty_usage("hermes_sessions_export_failed")
    return usage_from_hermes_sessions_jsonl(completed.stdout)


def usage_from_hermes_sessions_jsonl(jsonl: str) -> dict[str, Any]:
    usages: list[dict[str, Any]] = []
    for line in jsonl.splitlines():
        try:
            session = json.loads(line)
        except Exception:
            continue
        if not isinstance(session, dict):
            continue
        input_tokens = _int_value(session, "input_tokens")
        cache_read_tokens = _int_value(session, "cache_read_tokens") or 0
        cache_write_tokens = _int_value(session, "cache_write_tokens") or 0
        prompt_tokens = None
        if input_tokens is not None:
            prompt_tokens = input_tokens + cache_read_tokens + cache_write_tokens
        output_tokens = _int_value(session, "output_tokens")
        reasoning_tokens = _int_value(session, "reasoning_tokens") or 0
        total_tokens = None
        if prompt_tokens is not None and output_tokens is not None:
            total_tokens = prompt_tokens + output_tokens + reasoning_tokens
        actual_cost = _float_value(session, "actual_cost_usd")
        estimated_cost = _float_value(session, "estimated_cost_usd")
        cost = actual_cost if actual_cost is not None else estimated_cost
        usages.append(
            {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": output_tokens,
                "reasoning_tokens": reasoning_tokens or None,
                "cache_read_tokens": cache_read_tokens or None,
                "cache_write_tokens": cache_write_tokens or None,
                "total_tokens": total_tokens,
                "cost_usd": cost,
                "cost_kind": "actual" if actual_cost is not None else ("estimated" if estimated_cost is not None else "unavailable"),
                "source": "hermes_sessions_export",
            }
        )
    return merge_usage(usages)


def _usage_from_records(records: Any, *, source: str) -> dict[str, Any]:
    usages = []
    for record in records or []:
        if not isinstance(record, dict):
            continue
        normalized: dict[str, Any] | None = None
        for candidate in _usage_candidates(record):
            candidate_usage = _normalize_usage_dict(candidate, source=source)
            if normalized is None:
                normalized = candidate_usage
            else:
                for key, value in candidate_usage.items():
                    if key not in {"source", "cost_kind"} and normalized.get(key) is None and value is not None:
                        normalized[key] = value
                if normalized.get("cost_usd") is None and candidate_usage.get("cost_usd") is not None:
                    normalized["cost_usd"] = candidate_usage["cost_usd"]
                    normalized["cost_kind"] = candidate_usage["cost_kind"]
            if any(normalized.get(key) is not None for key in ("prompt_tokens", "completion_tokens", "total_tokens", "cost_usd")):
                continue
        if normalized is None:
            normalized = empty_usage(source)
        record_cost = _float_value(record, "actual_cost_usd", "cost_usd", "cost")
        if record_cost is not None:
            normalized["cost_usd"] = record_cost
            normalized["cost_kind"] = "actual" if _float_value(record, "actual_cost_usd") is not None else "estimated"
        if any(normalized.get(key) is not None for key in ("prompt_tokens", "completion_tokens", "total_tokens", "cost_usd")):
            usages.append(normalized)
    return merge_usage(usages)


def _hermes_session_id(stdout: str) -> str:
    matches = re.findall(r"\bSession:\s*([0-9]{8}_[0-9]{6}_[A-Za-z0-9]+)", stdout)
    return matches[-1] if matches else ""


def _hermes_command_prefix(command: list[str]) -> list[str] | None:
    for index, item in enumerate(command):
        if Path(item).name != "hermes":
            continue
        if index > 0 and Path(command[index - 1]).name in {"python", "python3"}:
            return [command[index - 1], item]
        return [item]
    return None


def _usage_candidates(record: dict[str, Any]) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for key in ("usage", "token_usage", "usage_metadata"):
        value = record.get(key)
        if isinstance(value, dict):
            candidates.append(value)
    response = record.get("response")
    if isinstance(response, dict):
        for key in ("usage", "token_usage", "usage_metadata"):
            value = response.get(key)
            if isinstance(value, dict):
                candidates.append(value)
        hidden = response.get("_hidden_params")
        if isinstance(hidden, dict):
            candidates.append(hidden)
    hidden = record.get("_hidden_params")
    if isinstance(hidden, dict):
        candidates.append(hidden)
    return candidates


def _normalize_usage_dict(raw: dict[str, Any], *, source: str) -> dict[str, Any]:
    prompt_tokens = _int_value(raw, "prompt_tokens", "input_tokens")
    completion_tokens = _int_value(raw, "completion_tokens", "output_tokens")
    prompt_details = raw.get("prompt_tokens_details") if isinstance(raw.get("prompt_tokens_details"), dict) else {}
    completion_details = raw.get("completion_tokens_details") if isinstance(raw.get("completion_tokens_details"), dict) else {}
    cache_read_tokens = _int_value(raw, "cache_read_tokens", "cached_tokens")
    if cache_read_tokens is None:
        cache_read_tokens = _int_value(prompt_details, "cached_tokens")
    cache_write_tokens = _int_value(raw, "cache_write_tokens")
    reasoning_tokens = _int_value(raw, "reasoning_tokens")
    if reasoning_tokens is None:
        reasoning_tokens = _int_value(completion_details, "reasoning_tokens")
    total_tokens = _int_value(raw, "total_tokens")
    if total_tokens is None and prompt_tokens is not None and completion_tokens is not None:
        total_tokens = prompt_tokens + completion_tokens + (reasoning_tokens or 0)
    actual_cost = _float_value(raw, "actual_cost_usd", "cost_usd", "cost")
    estimated_cost = _float_value(raw, "estimated_cost_usd", "response_cost")
    cost = actual_cost if actual_cost is not None else estimated_cost
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "reasoning_tokens": reasoning_tokens,
        "cache_read_tokens": cache_read_tokens,
        "cache_write_tokens": cache_write_tokens,
        "total_tokens": total_tokens,
        "cost_usd": cost,
        "cost_kind": "actual" if actual_cost is not None else ("estimated" if estimated_cost is not None else "unavailable"),
        "source": source,
    }


def _int_value(raw: dict[str, Any], *keys: str) -> int | None:
    for key in keys:
        value = raw.get(key)
        if isinstance(value, int):
            return value
        if isinstance(value, float):
            return int(value)
    return None


def _float_value(raw: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        value = raw.get(key)
        if isinstance(value, int | float):
            return float(value)
    return None


def _has_usage(usage: dict[str, Any]) -> bool:
    return any(usage.get(key) is not None for key in ("prompt_tokens", "completion_tokens", "total_tokens", "cost_usd"))
