import hashlib,json
from types import SimpleNamespace
import pytest
import httpx
from competition.scientific_evidence_relay import Relay,allowed,PREFIX

R='round_20261122T151842Z_testnet';U='a'*32

@pytest.mark.parametrize('method,path',[('POST','/admin/competition/rounds'),('POST','/internal/competition/rounds/r/jobs/claim'),('GET',PREFIX+'recoveries/other/'+U),('GET',PREFIX+'heads/recovery:other:paper:x'),('DELETE',PREFIX+'blobs/'+'b'*64),('GET',PREFIX+'blobs/'+'b'*64+'?redirect=x')])
def test_relay_rejects_actions_outside_one_diagnostic_recovery(method,path):assert not allowed(method,path,R,U)

def test_lost_cas_reply_retries_exact_bytes_and_does_not_change_binding(monkeypatch):
    calls=[];body=b'{"expected_parent_hash":"old","new_hash":"new"}'
    monkeypatch.setattr('competition.scientific_evidence_relay.time.sleep',lambda _:None)
    def request(*args,**kwargs):
        calls.append((args,kwargs))
        if len(calls)==1:raise httpx.ReadError('lost reply')
        return SimpleNamespace(status_code=200,content=b'{"sha256":"new"}',headers={})
    relay=Relay(SimpleNamespace(request=request),R,U)
    assert relay.forward('POST',PREFIX+'heads/recovery:'+U+':paper:x',body,'Bearer private')[0]==200
    assert calls[0]==calls[1] and relay.retries==1

def test_relay_never_retries_a_conflict_or_auth_failure(monkeypatch):
    monkeypatch.setattr('competition.scientific_evidence_relay.time.sleep',lambda _:pytest.fail('No retry'))
    for status in (401,403,409):
        relay=Relay(SimpleNamespace(request=lambda *a,**k:SimpleNamespace(status_code=status,content=b'{}',headers={})),R,U)
        assert relay.forward('GET',PREFIX+'heads/model-operation:x',b'','Bearer private')[0]==status

def test_blob_cache_requires_matching_original_bytes_and_upstream_receipt():
    calls=[];raw=b'exact evidence';digest=hashlib.sha256(raw).hexdigest();path=PREFIX+'blobs/'+digest
    def request(*args,**kwargs):
        calls.append(args);return SimpleNamespace(status_code=200,content=json.dumps({'sha256':digest,'byte_length':len(raw)}).encode(),headers={})
    relay=Relay(SimpleNamespace(request=request),R,U)
    assert relay.forward('PUT',path,raw,'Bearer private')==relay.forward('PUT',path,raw,'Bearer private')
    assert len(calls)==1
    assert relay.forward('PUT',path,b'changed','Bearer private')[0]==400 and len(calls)==1
