"""Frozen default or explicitly configured competition calendar."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Mapping


@dataclass(frozen=True)
class RoundSchedule:
    deadline: datetime
    reveal_delay_blocks: int
    gold_close: datetime
    publish_at: datetime
    activate_at: datetime
    custom: bool
    replay_gold_window_seconds: int = 86400


def _utc(value: Any, name: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} is invalid") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{name} needs a timezone")
    return parsed.astimezone(UTC)


def schedule_for_round(record: Mapping[str, Any]) -> RoundSchedule:
    """Read custom gates from the immutable public rules, never host settings."""
    deadline = _utc(record["deadline_at"], "competition deadline")
    rules = record.get("rules_public") or {}
    policy = rules.get("policy") or {}
    custom = policy.get("schedule")
    if custom is None:
        if (deadline.weekday(), deadline.hour, deadline.minute, deadline.second,
            deadline.microsecond) != (0, 12, 0, 0, 0):
            raise ValueError("competition deadline must be Monday 12:00 UTC")
        return RoundSchedule(
            deadline, 600, deadline + timedelta(days=3),
            deadline + timedelta(days=3, hours=6),
            deadline + timedelta(days=7, hours=-3), False,
        )
    if (record.get("network") not in {"mainnet", "testnet"} or not isinstance(custom, dict)
        or not {"mode", "reveal_delay_blocks", "gold_close_at", "publish_at",
                "production_activate_at"} <= set(custom)
        or set(custom) - {
            "mode", "reveal_delay_blocks", "gold_close_at", "publish_at",
            "production_activate_at", "replay_gold_window_seconds",
        } or custom.get("mode") != "custom"):
        raise ValueError("custom schedule is invalid")
    delay = custom["reveal_delay_blocks"]
    if type(delay) is not int or not 1 <= delay <= 600:
        raise ValueError("custom reveal delay must be 1 to 600 blocks")
    replay_window = custom.get("replay_gold_window_seconds", 86400)
    if type(replay_window) is not int or not 60 <= replay_window <= 86400:
        raise ValueError("custom replay Gold window must be 60 to 86400 seconds")
    gold_close = _utc(custom["gold_close_at"], "Gold close")
    publish_at = _utc(custom["publish_at"], "publication")
    activate_at = _utc(custom["production_activate_at"], "production activation")
    if not deadline < gold_close < publish_at < activate_at:
        raise ValueError("custom schedule gates are out of order")
    return RoundSchedule(deadline, delay, gold_close, publish_at,
                         activate_at, True, replay_window)


def validate_dispute_window(
    record: Mapping[str, Any], schedule: RoundSchedule,
) -> None:
    policy = (record.get("rules_public") or {}).get("policy") or {}
    window = policy.get("duplicate_dispute_window")
    if not isinstance(window, dict) or set(window) != {"opens_at", "closes_at"}:
        raise ValueError("public duplicate dispute window is required")
    opened = _utc(window["opens_at"], "duplicate dispute opening")
    closed = _utc(window["closes_at"], "duplicate dispute closing")
    verdict = _utc(policy.get("duplicate_verdict_public_at"), "duplicate verdict publication")
    if schedule.custom:
        if not schedule.deadline < opened == verdict < closed < schedule.gold_close:
            raise ValueError("custom duplicate dispute gates are out of order")
    elif (opened != schedule.deadline + timedelta(days=1, hours=-3)
          or verdict != opened
          or closed != schedule.deadline + timedelta(days=1, hours=3)):
        raise ValueError("duplicate verdict and dispute window must be Tuesday 09:00–15:00 UTC")
