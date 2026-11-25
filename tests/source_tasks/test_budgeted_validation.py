"""Real batching, durable failures, isolated roles and honest retrieval coverage."""
from concurrent.futures import ThreadPoolExecutor
import copy
import json
import time

import pytest

from tests.source_tasks.test_docling_frame import fixture
from tests.source_tasks.test_source_scoring import generated, packet_case  # noqa: F401
from validator.scientific_result_v1.source_tasks.budgeted_validation import PROFILE, Context, prepare, match
from validator.scientific_result_v1.source_tasks.budgeted_batches import pooled, decode_result, case_key
from validator.scientific_result_v1.source_tasks.contracts import MatchResponse, JudgeResponse
from validator.scientific_result_v1.source_tasks.failure_tolerance import policy
from validator.scientific_result_v1.source_tasks.operations import Operations, Hold
from validator.scientific_result_v1.source_tasks.packages import Package
from validator.scientific_result_v1.source_tasks.packets import Index


def wire(case,result):
    return dict(case_id=case['case_id'],result_json=json.dumps(result))


def optimized(tmp_path,monkeypatch):
    store,source,base=fixture(tmp_path,monkeypatch,context_window_tokens=262144)
    ops=Operations(store=store,routes=base.routes,runtimes=base.runtimes,tokenizers=base.tokenizers,
        trace_dir=base.trace_dir,deadline=time.time()+60,policy_hash=base.policy_hash,
        failure_tolerance_policy=policy(),budgeted_profile=dict(PROFILE))
    return store,source,ops


def request(alias, task):
    return dict(task={'task_id':task},output_alias=alias,inspection_component_ids=['/results/0'],
        required_requirement_ids=['r'],components={'/results/0':{'literal':'3.5000'}},
        authoritative_evidence={'anchors':[dict(anchor_id='a',exact_text='Reported mean 3.5000.')]})


def test_multiple_findings_share_one_paid_batch_and_completed_items_resume(tmp_path,monkeypatch):
    store,_,ops=optimized(tmp_path,monkeypatch);calls=[]
    class Runtime:
        def generate(self,*,payload,**kwargs):
            calls.append(payload)
            return {'items':[wire(c,dict(
                task_id=c['payload']['task']['task_id'],output_alias=c['payload']['output_alias'],
                state='MATCHED',inspected_component_ids=['/results/0'],matches=[dict(requirement_id='r',
                output_refs=[dict(kind='json',output_alias=c['payload']['output_alias'],pointer='/results/0')])],
                explanation='Observed original match')) for c in payload['cases']]}
    ops.runtimes['PRIMARY']=Runtime()
    inputs=[request('miner-a','task-'+str(i)) for i in range(4)]
    def call(data):return ops.scoped().request('PRIMARY','source_match',data,MatchResponse)
    with ThreadPoolExecutor(max_workers=4) as pool:results=list(pool.map(call,inputs))
    assert len(calls)==1 and len(results)==4
    assert len(calls[0]['shared_source_anchors'])==1 and len(calls[0]['shared_values'])==1
    assert [r.task_id for r in results]==['task-'+str(i) for i in range(4)]
    assert [call(data) for data in inputs]==results
    assert len(calls)==1


def test_one_invalid_case_does_not_discard_valid_siblings_or_retry(tmp_path,monkeypatch):
    _,_,ops=optimized(tmp_path,monkeypatch);calls=[]
    class Runtime:
        def generate(self,*,payload,**kwargs):
            calls.append(1)
            return {'items':[wire(c,dict(task_id=c['payload']['task']['task_id'],
                output_alias='a',state='ABSENT',inspected_component_ids=['/results/0'],
                matches=[dict(requirement_id='r',output_refs=[])],explanation='Empty shortlist')) for c in payload['cases']]}
    ops.runtimes['PRIMARY']=Runtime()
    def call(i):
        def validate(response):
            if response.task_id=='bad':raise ValueError('Wrong retained witness')
        return ops.scoped().request('PRIMARY','source_match',request('a',i),MatchResponse,validate=validate)
    with ThreadPoolExecutor(max_workers=2) as pool:
        good,bad=[pool.submit(call,i) for i in ['good','bad']]
        assert good.result().task_id=='good'
        with pytest.raises(ValueError):bad.result()
    with pytest.raises(Hold,match='disabled retries'):call('bad')
    assert call('good').task_id=='good' and len(calls)==1


def test_failed_provider_batch_is_retained_without_repeat_dispatch(tmp_path,monkeypatch):
    _,_,ops=optimized(tmp_path,monkeypatch);calls=[];ops.max_provider_attempts=1
    # scoped() derives the same owner control, rather than silently restoring retries.
    ops.diagnostic_runtime_controls=dict(kind='claims_local_diagnostic_runtime_controls_v1',
        shared_max_llm_requests=500,max_provider_attempts=1)
    class Runtime:
        def generate(self,**kwargs):calls.append(1);raise TimeoutError('Network failure')
    ops.runtimes['PRIMARY']=Runtime()
    for _ in range(2):
        with pytest.raises(Hold):ops.scoped().request('PRIMARY','source_match',request('a','t'),MatchResponse)
    assert len(calls)==1


def test_judges_and_miners_have_separate_batches_and_secondary_is_blind(tmp_path,monkeypatch):
    _,_,ops=optimized(tmp_path,monkeypatch);calls=[]
    class Runtime:
        def __init__(self,role):self.role=role
        def generate(self,*,payload,**kwargs):
            cases=payload['cases'];aliases={c['payload']['packet']['output_alias'] for c in cases}
            assert len(aliases)==1
            if self.role=='SECONDARY':
                assert all('primary' not in c['payload'] and 'secondary' not in c['payload'] for c in cases)
            calls.append((self.role,next(iter(aliases)),len(cases)))
            return {'items':[wire(c,dict(task_id=c['payload']['task']['task_id'],
                packet_id=c['payload']['packet']['packet_id'],outcome='SOURCE_UNRESOLVED',checks=[],
                evidence_requests=[],target_issue=None,unresolved_reason='Unreported uncertainty type',
                technical_error=None)) for c in cases]}
    for role in ['PRIMARY','SECONDARY']:ops.runtimes[role]=Runtime(role)
    def call(item):
        role,alias,i=item
        data=dict(task={'task_id':str(i)},packet={'packet_id':'packet-'+str(i),'output_alias':alias,'objects':[]},
                  authoritative_evidence={'anchors':[]})
        return ops.scoped().request(role,'primary_verify' if role=='PRIMARY' else 'secondary',data,JudgeResponse)
    jobs=[(role,alias,i) for role in ['PRIMARY','SECONDARY'] for alias in ['a','b'] for i in range(2)]
    with ThreadPoolExecutor(max_workers=8) as pool:results=list(pool.map(call,jobs))
    assert len(results)==8 and len(calls)==4 and all(n==2 for _,_,n in calls)


def test_shortlist_never_certifies_absence(generated,tmp_path,monkeypatch):
    path,_,_=generated;index=Index(Package(path,'a'));store,_,ops=optimized(tmp_path,monkeypatch)
    task,_=packet_case(index,store,refs=[index.objects[0].original_ref])
    class Runtime:
        def request(self,role,operation,data,response_type,*,validate):
            result=MatchResponse(task_id=task.task_id,output_alias='a',state='ABSENT',
                inspected_component_ids=data['inspection_component_ids'],
                matches=[dict(requirement_id=r.requirement_id,output_refs=[]) for r in task.requirements],
                explanation='Not in shortlist')
            validate(result);return result
    fake=Runtime();fake.budgeted_profile=PROFILE;fake.store=store
    with pytest.raises(Hold,match='no exhaustive omission'):match(index,task,fake,{'anchors':[]},'PRIMARY')


def test_pooling_keeps_independent_judges_and_original_evidence_scoped():
    a=request('a','one');b=request('a','two')
    b['authoritative_evidence']['anchors'][0]['exact_text']='Different condition, 3.5000 SEM.'
    original=copy.deepcopy([a,b]);batch=pooled([dict(key=str(i),data=d) for i,d in enumerate([a,b])])
    assert [a,b]==original and len(batch['shared_source_anchors'])==2
    refs=[case['payload']['authoritative_evidence']['shared_source_anchor_refs'] for case in batch['cases']]
    assert refs[0]!=refs[1]
    assert batch['shared_source_anchors'][refs[0][0]]['exact_text']=='Reported mean 3.5000.'


def test_local_source_lookup_keeps_complete_small_scope_and_zero_llm_searches(generated,tmp_path,monkeypatch):
    from tests.source_tasks.test_evidence_batching import case
    index,store,ops,frame,task,packet=case(generated,tmp_path,monkeypatch)
    frame=frame.model_copy(update={'anchors':[a.model_copy(update={'exact_text':a.exact_text[:300]}) for a in frame.anchors]})
    ops.budgeted_profile=dict(PROFILE);ops.budgeted_context=Context()
    result=prepare(ops,frame,[(task,packet)])
    _,rendered,evidence,summary=result[0]
    assert summary['complete_for_scope'] is True and summary['independent_roles']==[]
    assert rendered.evidence_anchor_ids==task.evidence_anchor_ids
    assert evidence['anchors']==[a.model_dump(mode='json') for a in frame.anchors]
    assert not ops.used


def test_mode_cannot_enable_without_diagnostic_or_with_unknown_profile(tmp_path,monkeypatch):
    _,_,ops=optimized(tmp_path,monkeypatch)
    kwargs=dict(store=ops.store,routes=ops.routes,runtimes=ops.runtimes,tokenizers=ops.tokenizers,
        trace_dir=ops.trace_dir,deadline=ops.deadline,policy_hash=ops.policy_hash)
    with pytest.raises(ValueError,match='local DSPy diagnostic'):Operations(**kwargs,budgeted_profile=PROFILE)
    with pytest.raises(ValueError,match='Unknown budgeted'):Operations(**kwargs,
        failure_tolerance_policy=policy(),budgeted_profile={**PROFILE,'candidate_components':1})
    assert ops.scoped().budgeted_context is ops.budgeted_context


def test_keyed_wire_decoding_is_lossless_and_never_supplies_a_missing_decision():
    data=request('a','t')
    raw=dict(task_id='t',output_alias='a',state='MATCHED',inspected_component_ids=['/results/0'],
        matches={'r':[dict(kind='json',output_alias='a',pointer='/results/0')]},explanation='Original reference')
    result=decode_result(json.dumps(raw),MatchResponse,data)
    assert result.matches[0].output_refs[0].pointer=='/results/0'
    assert result.matches[0].requirement_id=='r'
    with pytest.raises(ValueError,match='every requirement'):decode_result(json.dumps({**raw,'matches':{}}),MatchResponse,data)
    with pytest.raises(ValueError):decode_result(json.dumps(raw)+' trailing',MatchResponse,data)
    duplicate=json.dumps(raw).replace('"output_alias": "a"','"output_alias": "a", "output_alias": "b"',1)
    with pytest.raises(ValueError):decode_result(duplicate,MatchResponse,data)


def test_decoder_correction_recovers_paid_response_without_another_provider_call(tmp_path,monkeypatch):
    store,_,ops=optimized(tmp_path,monkeypatch);calls=[]
    class Runtime:
        def generate(self,*,payload,**kwargs):
            calls.append(1)
            return {'items':[wire(c,dict(task_id='t',output_alias='a',state='ABSENT',
                inspected_component_ids=['/results/0'],matches={'r':[]},explanation='No shortlist match'))
                for c in payload['cases']]}
    ops.runtimes['PRIMARY']=Runtime();data=request('a','t')
    ops.request('PRIMARY','source_match',data,MatchResponse)
    key=case_key(ops,'PRIMARY','source_match',data);head=store.head('budgeted-case:'+key)
    saved=json.loads(store.get_blob(head));saved.update(result=None,error='ValidationError')
    broken=store.put(saved);store.advance('budgeted-case:'+key,head,broken.sha256)
    recovered=ops.request('PRIMARY','source_match',data,MatchResponse)
    assert recovered.matches[0].output_refs==[] and len(calls)==1
    saved=json.loads(store.get_blob(store.head('budgeted-case:'+key)))
    assert saved['wire_recovery']['previous_receipt_sha256']==broken.sha256
    assert saved['wire_recovery']['provider_calls']==0


def test_ready_batches_for_same_miner_dispatch_concurrently(tmp_path,monkeypatch):
    from threading import Event,Lock
    _,_,ops=optimized(tmp_path,monkeypatch);calls=[];guard=Lock();both=Event()
    class Runtime:
        def generate(self,*,payload,**kwargs):
            with guard:
                calls.append(1)
                if len(calls)>=2:both.set()
            assert both.wait(2),'Independent batches were serialized'
            return {'items':[wire(c,dict(task_id=c['payload']['task']['task_id'],output_alias='a',
                state='ABSENT',inspected_component_ids=['/results/0'],matches={'r':[]},explanation='No hit'))
                for c in payload['cases']]}
    ops.runtimes['PRIMARY']=Runtime()
    def call(i):return ops.scoped().request('PRIMARY','source_match',request('a',str(i)),MatchResponse)
    with ThreadPoolExecutor(max_workers=8) as pool:results=list(pool.map(call,range(8)))
    assert len(results)==8 and len(calls)==2
