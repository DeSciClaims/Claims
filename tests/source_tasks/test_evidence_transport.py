from types import SimpleNamespace
from urllib.error import HTTPError
import pytest
from validator.scientific_result_v1.source_tasks.evidence_transport import evidence_request,retry_evidence

@pytest.mark.parametrize('failure',[OSError('lost reply'),RuntimeError('competition backend is unavailable'),RuntimeError('competition backend returned HTTP 503'),HTTPError('https://example.test',429,'busy',{},None)])
def test_only_retryable_transport_errors_retry_with_bounded_backoff(monkeypatch,failure):
    waits=[];calls=[]
    monkeypatch.setattr('validator.scientific_result_v1.source_tasks.evidence_transport.time.sleep',waits.append)
    def attempt():
        calls.append(True)
        if len(calls)<3:raise failure
        return {'sha256':'same'}
    assert retry_evidence(attempt)=={'sha256':'same'}
    assert len(calls)==3 and waits==[1,2]

@pytest.mark.parametrize('status',[400,401,403,404,409,422])
def test_identity_authorization_and_cas_conflicts_are_never_retried(monkeypatch,status):
    monkeypatch.setattr('validator.scientific_result_v1.source_tasks.evidence_transport.time.sleep',lambda _:pytest.fail('No retry'))
    def attempt():raise HTTPError('https://example.test',status,'rejected',{},None)
    with pytest.raises(HTTPError):retry_evidence(attempt)

def test_evidence_retry_never_retries_a_worker_claim_or_round_mutation():
    backend=SimpleNamespace(_request=lambda *args:pytest.fail('Must reject before issuing request'))
    with pytest.raises(ValueError):evidence_request(backend,'POST','/internal/competition/rounds/r/jobs/claim',{})
    with pytest.raises(ValueError):evidence_request(backend,'POST','/internal/competition/scientific/something-new',{})

def test_idempotent_cas_lost_reply_replays_exact_predecessor_and_child(monkeypatch):
    calls=[];child={'expected_parent_hash':'old','new_hash':'new'}
    monkeypatch.setattr('validator.scientific_result_v1.source_tasks.evidence_transport.time.sleep',lambda _:None)
    def request(*args):
        calls.append(args)
        if len(calls)==1:raise RuntimeError('competition backend is unavailable')
        return {'sha256':'new'}
    assert evidence_request(SimpleNamespace(_request=request),'POST','/internal/competition/scientific/heads/op',child)=={'sha256':'new'}
    assert calls[0]==calls[1]


def test_confirmed_immutable_blob_is_uploaded_once_per_remote_store(tmp_path,monkeypatch):
    import json
    from validator.scientific_result_v1.source_tasks.remote import RemoteStore
    from validator.scientific_result_v1.source_tasks.store import sha
    store=RemoteStore(tmp_path,SimpleNamespace());calls=[]
    def upload(method,path,raw):
        calls.append((method,path,raw));return json.dumps({'sha256':sha(raw),'byte_length':len(raw)}).encode()
    monkeypatch.setattr(store,'_binary',upload)
    raw=b'original immutable bytes'
    assert store.put_blob(sha(raw),raw)==store.put_blob(sha(raw),raw)
    assert len(calls)==1
    with pytest.raises(ValueError,match='hash'):store.put_blob(sha(raw),b'changed bytes')
    assert len(calls)==1


def test_failed_upload_is_not_marked_confirmed(tmp_path,monkeypatch):
    import json
    from validator.scientific_result_v1.source_tasks.remote import RemoteStore
    from validator.scientific_result_v1.source_tasks.store import sha
    store=RemoteStore(tmp_path,SimpleNamespace());calls=[];raw=b'original'
    def upload(*args):
        calls.append(args)
        if len(calls)==1:raise OSError('Lost reply')
        return json.dumps({'sha256':sha(raw),'byte_length':len(raw)}).encode()
    monkeypatch.setattr(store,'_binary',upload)
    with pytest.raises(OSError):store.put_blob(sha(raw),raw)
    store.put_blob(sha(raw),raw);assert len(calls)==2
