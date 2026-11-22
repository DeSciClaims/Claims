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


def test_lineage_reuses_immutable_blobs_but_refreshes_remote_heads(tmp_path,monkeypatch):
    from competition.scientific_lineage import evidence_store
    from validator.scientific_result_v1.source_tasks.remote import RemoteStore
    from validator.scientific_result_v1.source_tasks.store import sha
    monkeypatch.setenv('CLAIMS_SCIENTIFIC_EVIDENCE_ROOT',str(tmp_path))
    downloads=[];heads=[];current=['first']
    raw=b'original immutable evidence';digest=sha(raw)
    def binary(self,method,path,body=None):
        assert method=='GET' and path.endswith(digest)
        downloads.append(path);return raw
    def request(*args):
        heads.append(current[0]);return {'sha256':current[0]}
    monkeypatch.setattr(RemoteStore,'_binary',binary)
    backend=SimpleNamespace(base_url='https://development.example',_request=request)
    with evidence_store(backend) as store:
        assert store.get_blob(digest)==raw
        assert store.head('scientific-plan:round:final')=='first'
    current[0]='second'
    with evidence_store(backend) as store:
        assert store.get_blob(digest)==raw
        assert store.head('scientific-plan:round:final')=='second'
    assert len(downloads)==1
    assert heads==['first','second']
