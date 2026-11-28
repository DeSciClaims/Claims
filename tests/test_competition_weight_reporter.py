import json
from contextlib import contextmanager
import pytest

from neurons.competition_weight_reporter import report


def test_report_uses_only_saved_submission_and_is_safe_to_repeat(tmp_path, monkeypatch):
    saved = {'round_id': 'round', 'publication_sha256': 'a' * 64,
             'submitted_at': '2026-11-28T01:00:00Z', 'submitted_block': 100,
             'weights': [{'uid': 0, 'weight': .3788, 'hotkey': None, 'is_burn': True}]}
    state = {'submitted_weights': saved, 'last_submitted_at': saved['submitted_at'],
             'last_submitted_block': 100, 'latest_competition_receipt_sha256': 'a' * 64}
    path = tmp_path / 'state.json'
    path.write_text(json.dumps(state))
    calls = []
    @contextmanager
    def upload(request, timeout):
        from io import BytesIO
        calls.append(request)
        yield BytesIO(b'{}')
    monkeypatch.setattr('urllib.request.urlopen', upload)
    assert report(path, 'https://api.example', 'secret')['status'] == 'reported'
    assert calls[0].full_url.endswith('/rounds/round/submitted-weights')
    assert json.loads(calls[0].data) == {k: v for k, v in saved.items() if k != 'round_id'}
    assert json.loads(path.read_text()) == state
    state['last_submitted_block'] = 101
    path.write_text(json.dumps(state))
    with pytest.raises(ValueError, match='differs'):
        report(path, 'https://api.example', 'secret')
    assert len(calls) == 1
