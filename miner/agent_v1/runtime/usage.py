from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import subprocess
from datetime import datetime
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


def usage_from_cli_process(
    command: list[str],
    stdout: str,
    stderr: str,
    *,
    cwd: str | Path | None = None,
    started_at: datetime | None = None,
    model: str = "",
) -> dict[str, Any]:
    hermes_machine_usage = usage_from_hermes_machine_output(stderr)
    if _has_usage(hermes_machine_usage):
        return hermes_machine_usage

    codex_usage = usage_from_codex_jsonl(stdout)
    if _has_usage(codex_usage):
        return _estimate_cost_from_env(codex_usage, model=model)

    claude_usage = usage_from_claude_jsonl(stdout)
    if _has_usage(claude_usage):
        return _estimate_cost_from_env(claude_usage, model=model)

    hermes_usage = usage_from_hermes_stdout(stdout, command_prefix=_hermes_command_prefix(command))
    if _has_usage(hermes_usage):
        return hermes_usage

    executable = _cli_executable(command)
    if executable == "hermes" and cwd is not None:
        stored_usage = usage_from_hermes_session_store(cwd, started_at=started_at)
        if _has_usage(stored_usage):
            return stored_usage
    if executable == "codex":
        stored_usage = usage_from_codex_session_store(stdout)
        if _has_usage(stored_usage):
            return _estimate_cost_from_env(stored_usage, model=model)
    if executable == "claude" and cwd is not None:
        stored_usage = usage_from_claude_session_store(cwd, started_at=started_at)
        if _has_usage(stored_usage):
            return _estimate_cost_from_env(stored_usage, model=model)

    if executable == "codex":
        return codex_usage
    if executable == "hermes":
        return hermes_usage
    if executable == "claude":
        return claude_usage
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


def usage_from_codex_session_store(stdout: str, *, codex_home: str | Path | None = None) -> dict[str, Any]:
    thread_id = _codex_thread_id(stdout)
    if not thread_id:
        return empty_usage("codex_thread_not_found")
    root = Path(codex_home or os.getenv("CODEX_HOME", "~/.codex")).expanduser() / "sessions"
    if not root.is_dir():
        return empty_usage("codex_session_store_not_found")
    files = list(root.rglob(f"*{thread_id}*.jsonl"))
    if not files:
        return empty_usage("codex_session_not_found")
    latest_usage: dict[str, Any] | None = None
    try:
        with max(files, key=lambda path: path.stat().st_mtime_ns).open(encoding="utf-8") as handle:
            for line in handle:
                try:
                    event = json.loads(line)
                except Exception:
                    continue
                payload = event.get("payload") if isinstance(event, dict) else None
                if not isinstance(payload, dict) or payload.get("type") != "token_count":
                    continue
                info = payload.get("info")
                raw = info.get("total_token_usage") if isinstance(info, dict) else None
                if isinstance(raw, dict):
                    latest_usage = raw
    except OSError:
        return empty_usage("codex_session_read_failed")
    if latest_usage is None:
        return empty_usage("codex_session_usage_not_found")
    return {
        "prompt_tokens": _int_value(latest_usage, "input_tokens"),
        "completion_tokens": _int_value(latest_usage, "output_tokens"),
        "reasoning_tokens": _int_value(latest_usage, "reasoning_output_tokens"),
        "cache_read_tokens": _int_value(latest_usage, "cached_input_tokens"),
        "cache_write_tokens": None,
        "total_tokens": _int_value(latest_usage, "total_tokens"),
        "cost_usd": None,
        "cost_kind": "unavailable",
        "source": "codex_session_store",
    }


def usage_from_claude_jsonl(stdout: str) -> dict[str, Any]:
    result_usage: dict[str, Any] | None = None
    message_usages: dict[str, dict[str, Any]] = {}
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except Exception:
            continue
        if not isinstance(event, dict):
            continue
        if event.get("type") == "result":
            raw = event.get("usage")
            if isinstance(raw, dict):
                result_usage = _normalize_claude_usage(raw, source="claude_stream_json")
                cost = _float_value(event, "total_cost_usd", "cost_usd")
                if cost is not None:
                    result_usage["cost_usd"] = cost
                    result_usage["cost_kind"] = "actual"
            continue
        message = event.get("message")
        if event.get("type") != "assistant" or not isinstance(message, dict):
            continue
        raw = message.get("usage")
        if not isinstance(raw, dict):
            continue
        identity = str(message.get("id") or event.get("uuid") or len(message_usages))
        message_usages[identity] = _normalize_claude_usage(raw, source="claude_stream_json")
    if result_usage is not None:
        return result_usage
    return merge_usage(list(message_usages.values()))


def usage_from_claude_session_store(
    cwd: str | Path,
    *,
    started_at: datetime | None = None,
    claude_home: str | Path | None = None,
) -> dict[str, Any]:
    root = Path(claude_home or os.getenv("CLAUDE_CONFIG_DIR", "~/.claude")).expanduser() / "projects"
    if not root.is_dir():
        return empty_usage("claude_session_store_not_found")
    cwd_text = str(Path(cwd).resolve())
    minimum_mtime = started_at.timestamp() - 5.0 if started_at is not None else 0.0
    candidates: list[Path] = []
    try:
        for path in root.rglob("*.jsonl"):
            if path.stat().st_mtime >= minimum_mtime:
                candidates.append(path)
    except OSError:
        return empty_usage("claude_session_scan_failed")
    matched_lines: list[str] = []
    for path in sorted(candidates, key=lambda item: item.stat().st_mtime, reverse=True):
        lines: list[str] = []
        matches_cwd = False
        try:
            with path.open(encoding="utf-8") as handle:
                for line in handle:
                    lines.append(line)
                    try:
                        event = json.loads(line)
                    except Exception:
                        continue
                    if isinstance(event, dict) and event.get("cwd") == cwd_text:
                        matches_cwd = True
        except OSError:
            continue
        if matches_cwd:
            matched_lines.extend(lines)
    if not matched_lines:
        return empty_usage("claude_session_not_found")
    usage = usage_from_claude_jsonl("".join(matched_lines))
    if _has_usage(usage):
        usage["source"] = "claude_session_store"
    return usage


def usage_from_hermes_session_store(
    cwd: str | Path,
    *,
    started_at: datetime | None = None,
    hermes_home: str | Path | None = None,
) -> dict[str, Any]:
    db_path = Path(hermes_home or os.getenv("HERMES_HOME", "~/.hermes")).expanduser() / "state.db"
    if not db_path.is_file():
        return empty_usage("hermes_session_store_not_found")
    minimum_started = started_at.timestamp() - 5.0 if started_at is not None else 0.0
    try:
        connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5.0)
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """
            SELECT input_tokens, output_tokens, cache_read_tokens, cache_write_tokens,
                   reasoning_tokens, estimated_cost_usd, actual_cost_usd
            FROM sessions
            WHERE cwd = ? AND started_at >= ?
            ORDER BY started_at
            """,
            (str(Path(cwd).resolve()), minimum_started),
        ).fetchall()
        connection.close()
    except (OSError, sqlite3.Error):
        return empty_usage("hermes_session_store_read_failed")
    if not rows:
        return empty_usage("hermes_session_not_found")
    return usage_from_hermes_sessions_jsonl("\n".join(json.dumps(dict(row)) for row in rows))


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


def _normalize_claude_usage(raw: dict[str, Any], *, source: str) -> dict[str, Any]:
    input_tokens = _int_value(raw, "input_tokens") or 0
    cache_read_tokens = _int_value(raw, "cache_read_input_tokens", "cache_read_tokens") or 0
    cache_write_tokens = _int_value(raw, "cache_creation_input_tokens", "cache_write_tokens") or 0
    completion_tokens = _int_value(raw, "output_tokens", "completion_tokens")
    output_details = raw.get("output_tokens_details")
    reasoning_tokens = (
        _int_value(output_details, "thinking_tokens", "reasoning_tokens")
        if isinstance(output_details, dict)
        else None
    )
    prompt_tokens = input_tokens + cache_read_tokens + cache_write_tokens
    total_tokens = _int_value(raw, "total_tokens")
    if total_tokens is None and completion_tokens is not None:
        total_tokens = prompt_tokens + completion_tokens
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "reasoning_tokens": reasoning_tokens,
        "cache_read_tokens": cache_read_tokens or None,
        "cache_write_tokens": cache_write_tokens or None,
        "total_tokens": total_tokens,
        "cost_usd": _float_value(raw, "total_cost_usd", "cost_usd"),
        "cost_kind": (
            "actual"
            if _float_value(raw, "total_cost_usd", "cost_usd") is not None
            else "unavailable"
        ),
        "source": source,
    }


def _codex_thread_id(stdout: str) -> str:
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except Exception:
            continue
        if isinstance(event, dict) and event.get("type") == "thread.started":
            value = str(event.get("thread_id") or "").strip()
            if value:
                return value
    return ""


def _cli_executable(command: list[str]) -> str:
    for item in command:
        name = Path(item).name
        if name in {"hermes", "codex", "claude"}:
            return name
    return Path(command[0]).name if command else ""


def _estimate_cost_from_env(usage: dict[str, Any], *, model: str) -> dict[str, Any]:
    if usage.get("cost_usd") is not None or not _has_usage(usage):
        return usage
    raw_pricing = os.getenv("CLAIMS_MODEL_PRICING_JSON", "").strip()
    if not raw_pricing:
        return usage
    try:
        pricing = json.loads(raw_pricing)
    except Exception:
        return usage
    if not isinstance(pricing, dict):
        return usage
    normalized_model = model.strip()
    model_pricing = pricing.get(normalized_model)
    if model_pricing is None and "/" in normalized_model:
        model_pricing = pricing.get(normalized_model.split("/", 1)[1])
    if not isinstance(model_pricing, dict):
        return usage
    input_rate = _float_value(model_pricing, "input", "input_per_million")
    output_rate = _float_value(model_pricing, "output", "output_per_million")
    if input_rate is None or output_rate is None:
        return usage
    cache_read_rate = _float_value(model_pricing, "cache_read", "cache_read_per_million")
    cache_write_rate = _float_value(model_pricing, "cache_write", "cache_write_per_million")
    prompt_tokens = int(usage.get("prompt_tokens") or 0)
    completion_tokens = int(usage.get("completion_tokens") or 0)
    cache_read_tokens = int(usage.get("cache_read_tokens") or 0)
    cache_write_tokens = int(usage.get("cache_write_tokens") or 0)
    uncached_tokens = max(0, prompt_tokens - cache_read_tokens - cache_write_tokens)
    cost = (
        uncached_tokens * input_rate
        + cache_read_tokens * (cache_read_rate if cache_read_rate is not None else input_rate)
        + cache_write_tokens * (cache_write_rate if cache_write_rate is not None else input_rate)
        + completion_tokens * output_rate
    ) / 1_000_000
    enriched = dict(usage)
    enriched["cost_usd"] = round(cost, 8)
    enriched["cost_kind"] = "estimated"
    source = str(enriched.get("source") or "").strip()
    enriched["source"] = f"{source}+configured_pricing" if source else "configured_pricing"
    return enriched


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
