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


@pytest.mark.parametrize('terminal_error',[None,'Provider unavailable'])
def test_remote_no_retry_contract_retains_one_attempt(tmp_path,monkeypatch,terminal_error):
    import json
    from validator.scientific_result_v1.source_tasks.remote import RemoteStore
    from validator.scientific_result_v1.source_tasks.store import Store,Exhausted
    store=RemoteStore(tmp_path,SimpleNamespace())
    monkeypatch.setattr(store,'put_blob',lambda digest,raw:Store.put_blob(store,digest,raw))
    monkeypatch.setattr(store,'head',lambda key:Store.head(store,key))
    monkeypatch.setattr(store,'advance',lambda key,parent,child:Store.advance(store,key,parent,child))
    request=store.put({'request':'original'})
    assert store.reserve('op',request.sha256,now=1,max_attempts=1)==(1,None)
    if terminal_error:store.finish('op',1,error=terminal_error)
    with pytest.raises(Exhausted,match='disabled retries'):
        store.reserve('op',request.sha256,now=1000,max_attempts=1)
    state=json.loads(store.get_blob(store.head('model-operation:op')))
    assert len(state['attempts'])==1
    if terminal_error is None:
        response=store.put({'result':'completed'})
        store.finish('op',1,response=response.sha256)
        assert store.reserve('op',request.sha256,now=1001,max_attempts=1)==(None,response.sha256)


def test_remote_legacy_retry_remains_bounded(tmp_path,monkeypatch):
    from validator.scientific_result_v1.source_tasks.remote import RemoteStore
    from validator.scientific_result_v1.source_tasks.store import Store,Pending,Exhausted
    store=RemoteStore(tmp_path,SimpleNamespace())
    monkeypatch.setattr(store,'put_blob',lambda digest,raw:Store.put_blob(store,digest,raw))
    monkeypatch.setattr(store,'head',lambda key:Store.head(store,key))
    monkeypatch.setattr(store,'advance',lambda key,parent,child:Store.advance(store,key,parent,child))
    ref=store.put({'request':'original'})
    assert store.reserve('op',ref.sha256,now=1,timeout=5)==(1,None)
    with pytest.raises(Pending):store.reserve('op',ref.sha256,now=2)
    store.finish('op',1,error='network')
    assert store.reserve('op',ref.sha256,now=3)==(2,None)
    store.finish('op',2,error='network')
    with pytest.raises(Exhausted):store.reserve('op',ref.sha256,now=4)
