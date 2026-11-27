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


def test_new_scientific_rounds_publish_after_checks_without_old_weekly_delay():
    from copy import deepcopy
    from competition.round_schedule import publication_when_ready_policy
    record = _pilot()
    policy = record['rules_public']['policy']
    policy.update(validation_protocol='claims-source-task-v1', winner_selection='silver_tiebreak')
    policy['schedule'].pop('gold_close_at')
    policy['schedule']['publish_at'] = '2026-11-07T16:00:00+00:00'
    policy['schedule']['production_activate_at'] = '2026-11-11T09:00:00+00:00'
    before = deepcopy(policy)
    deadline = datetime.fromisoformat(record['deadline_at'])
    result = publication_when_ready_policy(policy, deadline)
    assert policy == before
    assert result['schedule']['publish_at'] == '2026-11-04T10:20:01+00:00'
    assert result['schedule']['production_activate_at'] == before['schedule']['production_activate_at']
    record['rules_public']['policy'] = result
    validate_dispute_window(record, schedule_for_round(record))
    assert publication_when_ready_policy(result, deadline) == result


def test_existing_frozen_schedule_and_legacy_review_are_not_reinterpreted():
    from competition.round_schedule import publication_when_ready_policy
    record = _pilot(); policy = record['rules_public']['policy']
    before = schedule_for_round(record)
    assert publication_when_ready_policy(policy, before.deadline) == policy
    assert schedule_for_round(record) == before
    policy['validation_protocol'] = 'claims-source-task-v1'
    with pytest.raises(ValueError, match='without miner review'):
        publication_when_ready_policy(policy, before.deadline)
