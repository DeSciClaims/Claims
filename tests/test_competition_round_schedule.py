from datetime import UTC, datetime

import pytest

from competition.round_schedule import schedule_for_round, validate_dispute_window


def _pilot():
    return {
        "network": "testnet", "deadline_at": "2026-11-04T10:00:00+00:00",
        "rules_public": {"policy": {
            "schedule": {
                "mode": "custom", "reveal_delay_blocks": 5,
                "gold_close_at": "2026-11-04T11:00:00+00:00",
                "publish_at": "2026-11-04T11:15:00+00:00",
                "production_activate_at": "2026-11-04T11:30:00+00:00",
            },
            "duplicate_verdict_public_at": "2026-11-04T10:10:00+00:00",
            "duplicate_dispute_window": {
                "opens_at": "2026-11-04T10:10:00+00:00",
                "closes_at": "2026-11-04T10:20:00+00:00",
            },
        }},
    }


def test_accelerated_testnet_gates_are_frozen_in_rules():
    record = _pilot()
    schedule = schedule_for_round(record)
    validate_dispute_window(record, schedule)
    assert schedule.reveal_delay_blocks == 5
    assert schedule.gold_close == datetime(2026, 11, 4, 11, tzinfo=UTC)
    assert schedule.publish_at == datetime(2026, 11, 4, 11, 15, tzinfo=UTC)
    assert schedule.activate_at == datetime(2026, 11, 4, 11, 30, tzinfo=UTC)


def test_custom_schedule_works_on_mainnet_and_rejects_reordered_gates():
    record = _pilot()
    record["network"] = "mainnet"
    assert schedule_for_round(record).reveal_delay_blocks == 5
    validate_dispute_window(record, schedule_for_round(record))
    record = _pilot()
    record["rules_public"]["policy"]["schedule"]["publish_at"] = "2026-11-04T10:59:00+00:00"
    with pytest.raises(ValueError, match="out of order"):
        schedule_for_round(record)
