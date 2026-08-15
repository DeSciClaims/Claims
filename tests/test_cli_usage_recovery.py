from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from miner.agent_v1.runtime.usage import (
    usage_from_claude_jsonl,
    usage_from_cli_process,
    usage_from_codex_session_store,
    usage_from_hermes_session_store,
)


def test_recovers_hermes_usage_from_session_store(tmp_path: Path) -> None:
    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir()
    db_path = hermes_home / "state.db"
    connection = sqlite3.connect(db_path)
    connection.execute(
        """
        CREATE TABLE sessions (
            cwd TEXT, started_at REAL, input_tokens INTEGER, output_tokens INTEGER,
            cache_read_tokens INTEGER, cache_write_tokens INTEGER, reasoning_tokens INTEGER,
            estimated_cost_usd REAL, actual_cost_usd REAL
        )
        """
    )
    stage = tmp_path / "stage"
    stage.mkdir()
    connection.execute(
        "INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (str(stage.resolve()), 100.0, 100, 20, 40, 10, 5, 0.0123, None),
    )
    connection.commit()
    connection.close()

    usage = usage_from_hermes_session_store(
        stage,
        started_at=datetime.fromtimestamp(99, timezone.utc),
        hermes_home=hermes_home,
    )

    assert usage["prompt_tokens"] == 150
    assert usage["completion_tokens"] == 20
    assert usage["reasoning_tokens"] == 5
    assert usage["total_tokens"] == 175
    assert usage["cost_usd"] == 0.0123
    assert usage["cost_kind"] == "estimated"


def test_recovers_codex_cumulative_usage_from_rollout(tmp_path: Path) -> None:
    codex_home = tmp_path / "codex"
    sessions = codex_home / "sessions" / "2026" / "08" / "15"
    sessions.mkdir(parents=True)
    thread_id = "01a002c9-7dad-7122-b46f-d52fdd321c77"
    rollout = sessions / f"rollout-{thread_id}.jsonl"
    rollout.write_text(
        json.dumps(
            {
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {
                        "total_token_usage": {
                            "input_tokens": 100,
                            "cached_input_tokens": 60,
                            "output_tokens": 25,
                            "reasoning_output_tokens": 5,
                            "total_tokens": 125,
                        }
                    },
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    usage = usage_from_codex_session_store(
        json.dumps({"type": "thread.started", "thread_id": thread_id}),
        codex_home=codex_home,
    )

    assert usage["prompt_tokens"] == 100
    assert usage["completion_tokens"] == 25
    assert usage["cache_read_tokens"] == 60
    assert usage["reasoning_tokens"] == 5
    assert usage["total_tokens"] == 125
    assert usage["source"] == "codex_session_store"


def test_parses_claude_stream_result_without_double_counting_messages() -> None:
    message = {
        "type": "assistant",
        "message": {
            "id": "msg_1",
            "usage": {
                "input_tokens": 10,
                "cache_read_input_tokens": 20,
                "cache_creation_input_tokens": 5,
                "output_tokens": 7,
                "output_tokens_details": {"thinking_tokens": 2},
            },
        },
    }
    result = {
        "type": "result",
        "total_cost_usd": 0.0042,
        "usage": {
            "input_tokens": 10,
            "cache_read_input_tokens": 20,
            "cache_creation_input_tokens": 5,
            "output_tokens": 7,
        },
    }

    usage = usage_from_claude_jsonl(
        "\n".join([json.dumps(message), json.dumps(message), json.dumps(result)])
    )

    assert usage["prompt_tokens"] == 35
    assert usage["completion_tokens"] == 7
    assert usage["total_tokens"] == 42
    assert usage["cost_usd"] == 0.0042
    assert usage["cost_kind"] == "actual"


def test_applies_configured_pricing_when_subscription_cli_has_no_cost(
    monkeypatch,
    tmp_path: Path,
) -> None:
    codex_home = tmp_path / "codex"
    sessions = codex_home / "sessions"
    sessions.mkdir(parents=True)
    thread_id = "01a002c9-7dad-7122-b46f-d52fdd321c77"
    (sessions / f"rollout-{thread_id}.jsonl").write_text(
        json.dumps(
            {
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {
                        "total_token_usage": {
                            "input_tokens": 1_000_000,
                            "cached_input_tokens": 500_000,
                            "output_tokens": 100_000,
                            "reasoning_output_tokens": 10_000,
                            "total_tokens": 1_100_000,
                        }
                    },
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setenv(
        "CLAIMS_MODEL_PRICING_JSON",
        json.dumps({"gpt-5.5": {"input": 2.0, "cache_read": 0.5, "output": 8.0}}),
    )

    usage = usage_from_cli_process(
        ["codex"],
        json.dumps({"type": "thread.started", "thread_id": thread_id}),
        "",
        model="gpt-5.5",
    )

    assert usage["cost_usd"] == 2.05
    assert usage["cost_kind"] == "estimated"
    assert usage["source"] == "codex_session_store+configured_pricing"
