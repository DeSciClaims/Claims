from neurons.competition_duplicate_appeals import NonBlockingAppealsBackend
import pytest


def test_publication_uses_backend_frozen_resolved_appeals_not_the_live_review_queue(monkeypatch):
    client = NonBlockingAppealsBackend(base_url='https://example.test', controller_token='secret')
    requests = []
    def call(rid, suffix, payload):
        requests.append((rid, suffix, payload))
        return {'schema_version':'claims_duplicate_appeal_snapshot_v1','round_id':rid,
                'pending_count':3,'disputes':[{'hotkey':'a','status':'upheld'}]}
    monkeypatch.setattr(client, 'runtime_request', call)
    assert client.list_duplicate_disputes('round') == [{'hotkey':'a','status':'upheld'}]
    assert requests == [('round', '/duplicate-disputes/publication-snapshot', {})]


def test_invalid_backend_snapshot_cannot_be_silently_used(monkeypatch):
    client = NonBlockingAppealsBackend(base_url='https://example.test', controller_token='secret')
    monkeypatch.setattr(client, 'runtime_request', lambda *_:{'schema_version':'claims_duplicate_appeal_snapshot_v1',
        'round_id':'other','disputes':[{'status':'pending'}]})
    with pytest.raises(ValueError):
        client.list_duplicate_disputes('round')
