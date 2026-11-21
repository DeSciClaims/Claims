"""Replay publication uses the verified snapshot's actual scoring protocol."""
from types import SimpleNamespace

import pytest

from competition.scientific_lineage import verify_current_grid


def test_legacy_replay_snapshot_does_not_require_scientific_backend_methods():
    assert verify_current_grid('round', 'final', backend=SimpleNamespace(), pairs=[],
        round_record={'round_id': 'round', 'rules_public': {}}) == []


def test_scientific_replay_snapshot_cannot_bypass_required_evidence():
    record = {'round_id': 'round', 'rules_public': {
        'policy': {'validation_protocol': 'claims-source-task-v1'}}}
    backend = SimpleNamespace(get_round=lambda _: {'rules_public': {}})
    with pytest.raises(ValueError, match='Scientific replay requires retained evidence'):
        verify_current_grid('round', 'final', backend=backend, pairs=[], round_record=record)
