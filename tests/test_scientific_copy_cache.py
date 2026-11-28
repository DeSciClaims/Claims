from contextlib import contextmanager
from types import SimpleNamespace
import pytest
from competition import scientific_scoring as scoring,scientific_lineage,scientific_first_round
from validator.scientific_result_v1.source_tasks.store import canonical,sha


def test_copy_diagnostic_reuses_retained_evidence_without_uploading_receipts(monkeypatch):
    payload={'paper':'p'};digest=sha(canonical(payload));source='a'*64
    receipt=SimpleNamespace(state='HOLD',binding=SimpleNamespace(round_id='r',stage='final',paper_id='p',source_packet_sha256=source),
        input_packages=['package'],model_dump=lambda **kw:payload)
    monkeypatch.setattr(scoring,'SampledSilverReceipt',SimpleNamespace(model_validate_json=lambda *a,**kw:receipt))
    calls=[]
    store=SimpleNamespace(load=lambda ref,kind:SimpleNamespace(source_packet_sha256=source))
    @contextmanager
    def retained(backend):calls.append('retained');yield store
    monkeypatch.setattr(scientific_lineage,'evidence_store',retained)
    monkeypatch.setattr(scientific_first_round,'configured_policy',lambda config,rid:{'same':'policy'})
    def verify(got,score_ref,receipt_ref,policy):
        assert got is store and receipt_ref.sha256==digest and receipt_ref.byte_length==len(canonical(payload))
        assert policy=={'same':'policy'};calls.append('verified')
    monkeypatch.setattr(scientific_first_round,'verify_score_receipt',verify)
    score_ref=dict(relative_path='blobs/'+'b'*64,sha256='b'*64,byte_length=1,media_type='application/json')
    silver={'sampled_receipt':payload,'receipt_sha256':digest,'first_round_score':score_ref}
    backend=SimpleNamespace(get_paper_evaluation=lambda *a:{'result_private':{'silver':silver}},get_round=lambda rid:{'rules_public':{'configuration':{}}})
    args=dict(round_id='r',stage='final',entries=[{'role':'reference','hotkey':'b','package_sha256':'x'},
        {'role':'challenger','hotkey':'h','package_sha256':'y'}],paper_ids=['p'],backend=backend)
    first=scoring.scientific_copy_diagnostics(**args);second=scoring.scientific_copy_diagnostics(**args)
    assert first==second and first['h']['is_copy'] is False and calls==['retained','verified','retained','verified']
    silver['receipt_sha256']='c'*64
    with pytest.raises(ValueError,match='stale/corrupt'):scoring.scientific_copy_diagnostics(**args)
