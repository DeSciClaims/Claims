"""A bounded, restart-safe base-burn override after one signed round result.

The signed allocation still determines recipients and vacant reward slots.
This operator-configured window changes only the size of the miner pool.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
import math
from pathlib import Path
import json
import re

from .publication import canonical_sha256


def utc(value):
    parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
    if not isinstance(parsed, datetime) or parsed.tzinfo is None:
        raise ValueError('Burn window timestamp must include a timezone')
    return parsed.astimezone(UTC)


@dataclass(frozen=True)
class BurnWindowPolicy:
    schema_version: str
    network: str
    netuid: int
    trigger_round_id: str
    base_burn_fraction: float
    duration_seconds: int
    wait_for_trigger: bool

    def __post_init__(self):
        if (self.schema_version != 'claims_burn_window_v1'
                or self.network not in {'mainnet', 'testnet'}
                or type(self.netuid) is not int or self.netuid < 1
                or not isinstance(self.trigger_round_id, str)
                or not re.fullmatch(r'[A-Za-z0-9_-]{1,120}', self.trigger_round_id)
                or type(self.base_burn_fraction) not in {float, int}
                or not math.isfinite(self.base_burn_fraction)
                or not 0 <= self.base_burn_fraction < 1
                or type(self.duration_seconds) is not int
                or not 1 <= self.duration_seconds <= 90 * 86400
                or type(self.wait_for_trigger) is not bool):
            raise ValueError('Invalid burn window policy')

    @classmethod
    def load(cls, path: Path):
        return cls(**json.loads(path.read_text()))

    @property
    def digest(self):
        return canonical_sha256(asdict(self))

    def validate_state(self, value):
        if not isinstance(value, dict) or set(value) != {
            'policy_sha256', 'trigger_round_id', 'trigger_receipt_sha256',
            'triggered_at', 'started_at', 'expires_at',
        }:
            raise ValueError('Invalid persisted burn window')
        if (value['policy_sha256'] != self.digest
                or value['trigger_round_id'] != self.trigger_round_id
                or not re.fullmatch(r'[0-9a-f]{64}', str(value['trigger_receipt_sha256']))):
            raise ValueError('Burn window differs from its persisted policy or trigger')
        triggered = utc(value['triggered_at'])
        if value['started_at'] is None and value['expires_at'] is None:
            return
        start, end = utc(value['started_at']), utc(value['expires_at'])
        if start < triggered or end - start != timedelta(seconds=self.duration_seconds):
            raise ValueError('Burn window start or expiry changed')

    def trigger(self, receipt_sha256, now):
        value = dict(policy_sha256=self.digest, trigger_round_id=self.trigger_round_id,
            trigger_receipt_sha256=receipt_sha256, triggered_at=utc(now).isoformat(),
            started_at=None, expires_at=None)
        self.validate_state(value)
        return value

    def start(self, value, accepted_at):
        self.validate_state(value)
        if value['started_at'] is not None:
            return value
        start = max(utc(accepted_at), utc(value['triggered_at']))
        return {**value, 'started_at': start.isoformat(),
                'expires_at': (start + timedelta(seconds=self.duration_seconds)).isoformat()}

    def report(self, value, now):
        if value is None:
            phase = 'waiting_for_signed_results'
        else:
            self.validate_state(value)
            if value['started_at'] is None:
                phase = 'awaiting_first_submission'
            else:
                if utc(now) < utc(value['started_at']):
                    raise ValueError('Clock precedes persisted burn window start')
                phase = 'active' if utc(now) < utc(value['expires_at']) else 'expired'
        return dict(policy_sha256=self.digest, trigger_round_id=self.trigger_round_id,
            phase=phase, base_burn_fraction=self.base_burn_fraction,
            duration_seconds=self.duration_seconds,
            started_at=value['started_at'] if value else None,
            expires_at=value['expires_at'] if value else None)
