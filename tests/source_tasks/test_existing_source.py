from types import SimpleNamespace

import pytest

from validator.scientific_result_v1.source_tasks.remote import RemoteStore, EvidenceTransportError
from validator.scientific_result_v1.source_tasks.store import sha


def test_existing_pdf_is_hash_checked_and_never_uploaded(tmp_path, monkeypatch):
    raw = b'%PDF' + b'x' * 6_000_000
    backend = SimpleNamespace(get_scientific_source=lambda r, p: {'source_sha256': sha(raw)})
    store = RemoteStore(tmp_path, backend)
    monkeypatch.setattr(store, '_binary', lambda *a: pytest.fail('PDF uploaded through API'))
    ref = store.put_existing_source('round', 'paper', raw)
    assert ref.sha256 == sha(raw) and ref.byte_length == len(raw)
    assert ref.media_type == 'application/pdf' and store.read(ref) == raw
    with pytest.raises(ValueError, match='frozen source'):
        store.put_existing_source('round', 'paper', b'changed bytes')


def test_cold_store_fetches_frozen_original_and_verifies_hash(tmp_path, monkeypatch):
    raw = b'%PDF original bytes'
    meta = {'source_url': 'https://approved.example/paper.pdf', 'source_sha256': sha(raw)}
    requests = []
    backend = SimpleNamespace(_request=lambda m, p: requests.append(p) or meta)
    store = RemoteStore(tmp_path, backend)
    def unavailable(*a): raise EvidenceTransportError(404)
    monkeypatch.setattr(store, '_binary', unavailable)
    monkeypatch.setattr(store, '_fetch_original', lambda m, d: raw)
    assert store.get_blob(sha(raw)) == raw
    assert len(requests) == 1 and 'source-references/' in requests[0]
    assert store.get_blob(sha(raw)) == raw and len(requests) == 1
    other = RemoteStore(tmp_path / 'corrupt', backend)
    monkeypatch.setattr(other, '_binary', unavailable)
    monkeypatch.setattr(other, '_fetch_original', lambda m, d: b'corrupt')
    with pytest.raises(ValueError, match='hash'):
        other.get_blob(sha(raw))


def test_authorization_failure_does_not_trigger_source_fallback(tmp_path, monkeypatch):
    backend = SimpleNamespace(_request=lambda *a: pytest.fail('auth failure bypassed'))
    store = RemoteStore(tmp_path, backend)
    def denied(*a): raise EvidenceTransportError(401)
    monkeypatch.setattr(store, '_binary', denied)
    with pytest.raises(EvidenceTransportError): store.get_blob('a' * 64)
