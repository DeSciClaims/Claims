from types import SimpleNamespace
from threading import Barrier, Event
import pytest
from scripts import prepare_scientific_sources as scheduler
from validator.scientific_result_v1.source_tasks.store import Store,sha


def fixture(tmp_path,monkeypatch,*,workers=1):
    monkeypatch.setenv('CLAIMS_SCIENTIFIC_EVIDENCE_ROOT',str(tmp_path))
    store=Store(tmp_path/'evidence')
    wrapper=SimpleNamespace(head=store.head,generation=store.generation,
        put_existing_source=lambda r,p,raw:store.put_named(p+'.pdf',raw,'application/pdf'))
    operations=[]
    def ops(*args):operations.append(args);return SimpleNamespace(deadline=0)
    policy=SimpleNamespace(evaluate_pool=SimpleNamespace(store=wrapper,operations_for_paper=ops))
    monkeypatch.setattr(scheduler,'create_scientific_policy',lambda *args:policy)
    sample=[{'paper_id':'f','stage':'final','position':0},
        {'paper_id':'s','stage':'screen','position':0},
        {'paper_id':'p2','stage':'prescreen','position':1},
        {'paper_id':'p1','stage':'prescreen','position':0}]
    backend=SimpleNamespace(list_sample=lambda _:sample,
        get_scientific_source=lambda *args:{'source_url':'https://example.test/paper.pdf','source_sha256':sha(b'pdf')})
    row={'round_id':'round','network':'mainnet','status':'screening','incumbent_hotkey':None,'configuration_sha256':'1'*64,
        'rules_public':{'configuration':{'scientific_scoring':{}},'policy':{
        'validation_protocol':'claims-source-task-v1','winner_selection':'silver_tiebreak',
        'experimental_launch':{'status':'experimental'},
        'execution':{'schema_version':'claims_competition_execution_v1','bronze_max_workers':workers,'scoring_max_workers':10,'decision_max_workers':20},
        'evaluation':{'schema_version':'claims_competition_evaluation_v1','mode':'production','required_challengers':1,'reference_baseline_stages':True,'stages':[
            {'name':'prescreen','papers':20,'advance_top':12,'max_per_team':2},
            {'name':'screen','papers':120,'advance_top':3,'max_per_team':1},{'name':'final','papers':500}]}}}}
    return row,backend,store,operations


def test_source_order_and_reuse_preserve_committed_prescreen(tmp_path,monkeypatch):
    row,backend,store,operations=fixture(tmp_path,monkeypatch)
    ref=store.put({'prepared':'original'});store.advance('prepared-source:round:p1',None,ref.sha256)
    calls=[]
    def prepare(**kw):calls.append(kw)
    monkeypatch.setattr(scheduler,'prepare_paper',prepare)
    result=scheduler.prepare_round(row,backend,SimpleNamespace(fetch=lambda *args:b'pdf'))
    assert [c['packet'].paper_id for c in calls]==['p2','s','f']
    assert [c['stages'] for c in calls]==[['prescreen'],['screen'],['final']]
    assert all(c['policy_sha256']=='1'*64 and c['packet'].state=='READY' for c in calls)
    assert store.head('prepared-source:round:p1')==ref.sha256
    assert result['reused']==1 and result['prepared']==3


def test_workers_overlap_and_drain_without_starting_future_work(tmp_path,monkeypatch):
    row,backend,store,operations=fixture(tmp_path,monkeypatch,workers=2)
    barrier=Barrier(2);stop=Event();calls=[]
    def prepare(**kw):
        calls.append(kw['packet'].paper_id)
        barrier.wait(timeout=3);stop.set()
    monkeypatch.setattr(scheduler,'prepare_paper',prepare)
    result=scheduler.prepare_round(row,backend,SimpleNamespace(fetch=lambda *args:b'pdf'),stop=stop)
    assert set(calls)=={'p1','p2'} and result['prepared']==2
    assert result['status']=='preparation_drained'


def test_one_paper_failure_does_not_discard_other_preparation(tmp_path,monkeypatch):
    row,backend,store,operations=fixture(tmp_path,monkeypatch)
    def prepare(**kw):
        if kw['packet'].paper_id=='p1':raise TypeError('private error text must not be logged')
    monkeypatch.setattr(scheduler,'prepare_paper',prepare)
    events=[]
    result=scheduler.prepare_round(row,backend,SimpleNamespace(fetch=lambda *args:b'pdf'),completed=events.append)
    assert result['errors']==1 and result['prepared']==3
    assert events[0]['error_type']=='TypeError' and 'private' not in str(events)


def test_refuses_sample_outside_frozen_stage_plan(tmp_path,monkeypatch):
    row,backend,store,operations=fixture(tmp_path,monkeypatch)
    backend.list_sample=lambda _:[{'paper_id':'other','stage':'invented'}]
    with pytest.raises(ValueError,match='frozen evaluation plan'):scheduler.ordered_jobs(backend,row)
