"""Recovery preserves exact evidence, successful work and immutable attempt limits."""
from types import SimpleNamespace

import pytest

from validator.scientific_result_v1.source_tasks.contracts import (
    EvidenceRequest,
    SourceAnchor,
    TextLocator,
)
from validator.scientific_result_v1.source_tasks.evaluator import expand_evidence
from validator.scientific_result_v1.source_tasks.operations import Hold
from validator.scientific_result_v1.source_tasks.store import Conflict, Exhausted, Store


def test_expired_dispatch_reuses_success_without_allocating_an_attempt(tmp_path):
    store=Store(tmp_path);request=store.put({'request':1});response=store.put({'answer':1})
    ordinal,_=store.reserve('op',request.sha256,now=100)
    store.finish('op',ordinal,response=response.sha256)
    assert store.reserve('op',request.sha256,now=10000,allow_new=False)==(None,response.sha256)
    with pytest.raises(Conflict):store.reserve('op',store.put({'request':2}).sha256,allow_new=False)
    with pytest.raises(Exhausted,match='deadline'):store.reserve('new',request.sha256,allow_new=False)
    with store.connection() as c:
        assert c.execute("SELECT count(*) FROM attempts WHERE operation='new'").fetchone()[0]==0


def test_remote_expired_dispatch_reuses_success_without_changing_heads(tmp_path):
    from validator.scientific_result_v1.source_tasks.remote import RemoteStore
    class MemoryRemote(RemoteStore):
        put_blob=Store.put_blob
        def head(self,key):return self.heads.get(key)
        def advance(self,key,parent,child):
            assert self.heads.get(key)==parent
            self.heads[key]=child
    store=MemoryRemote(tmp_path,SimpleNamespace());store.heads={}
    request=store.put({'request':1});response=store.put({'answer':1})
    n,_=store.reserve('op',request.sha256,now=100);store.finish('op',n,response=response.sha256)
    before=dict(store.heads)
    assert store.reserve('op',request.sha256,now=10000,allow_new=False)==(None,response.sha256)
    with pytest.raises(Exhausted,match='deadline'):store.reserve('new',request.sha256,allow_new=False)
    assert store.heads==before


def test_operations_replay_after_deadline_never_dispatches_another_call(tmp_path,monkeypatch):
    import time

    from tests.source_tasks.test_docling_frame import fixture
    from validator.scientific_result_v1.source_tasks.docling_frame import (
        EligibilityResponse,
    )
    _,_,ops=fixture(tmp_path,monkeypatch)
    data={'tiles':[{'tile_id':'tile'}]}
    first=ops.request('SOURCE_READER','source_eligibility',data,EligibilityResponse)
    ops.deadline=time.time()-1;ops.offline=True
    assert ops.request('SOURCE_READER','source_eligibility',data,EligibilityResponse)==first
    with pytest.raises(Hold,match='deadline'):
        ops.request('SOURCE_READER','source_eligibility',{'tiles':[{'tile_id':'new'}]},EligibilityResponse)


def source():
    anchors=[SourceAnchor(anchor_id=k,document_uid='d',source_sha256='1'*64,
        locator=TextLocator(kind='text',start=i,stop=i+1),exact_text=text,rendering=None)
        for i,(k,text) in enumerate([('owned','Estimate 3.5.'),('definition','Units mg.'),
            ('large','Unrelated lengthy appendix. '*1000),('header','Treatment | Outcome'),('row','A | 3.5')])]
    table=SimpleNamespace(anchor_ids=['header','row'],data_row_anchor_ids=['row'],
        header_anchor_ids=['header'],note_anchor_ids=[])
    return SimpleNamespace(anchors=anchors,tables=[table])


def test_expansion_skips_oversized_optional_context_and_keeps_requested_witness():
    frame=source();current={'anchors':[frame.anchors[0].model_dump(mode='json')]}
    request=EvidenceRequest(request_id='e',kind='DEFINITION',anchor_ids=['definition'],document_uid='d',query='appendix')
    # A deliberately small deterministic budget makes the full appendix unfit.
    expanded=expand_evidence(frame,current,[request],600,tokenizers={'test':lambda s:len(s)//4})
    ids=[a['anchor_id'] for a in expanded['anchors']]
    assert 'owned' in ids and 'definition' in ids and 'large' not in ids
    assert all(a==next(x for x in frame.anchors if x.anchor_id==a['anchor_id']).model_dump(mode='json') for a in expanded['anchors'])


def test_explicit_oversized_witness_is_held_without_truncation():
    frame=source();current={'anchors':[frame.anchors[0].model_dump(mode='json')]}
    request=EvidenceRequest(request_id='e',kind='ADJACENT_TEXT',anchor_ids=['large'],document_uid='d',query='appendix')
    with pytest.raises(Hold,match='Required source evidence exceeds'):
        expand_evidence(frame,current,[request],600,tokenizers={'test':lambda s:len(s)//4})


def test_requesting_table_headers_does_not_attach_unrequested_data_rows():
    frame=source();current={'anchors':[frame.anchors[0].model_dump(mode='json')]}
    request=EvidenceRequest(request_id='e',kind='TABLE_HEADERS',anchor_ids=['header'],document_uid='d',query='header')
    expanded=expand_evidence(frame,current,[request],2000,tokenizers={'test':lambda s:len(s)//4})
    assert [a['anchor_id'] for a in expanded['anchors']]==['owned','header']


@pytest.mark.parametrize('uncertain_secondary',[False,True])
def test_confirmed_inventory_defect_creates_replayable_common_frame_revision(tmp_path,monkeypatch,uncertain_secondary):
    import json

    from tests.source_tasks.test_docling_frame import fixture
    from validator.scientific_result_v1.source_tasks.contracts import (
        EvaluationBinding,
        FileRef,
        SourceFrame,
    )
    from validator.scientific_result_v1.source_tasks.docling_frame import (
        validate_docling_frame,
    )
    from validator.scientific_result_v1.source_tasks.evaluator import evidence_for
    from validator.scientific_result_v1.source_tasks.preparation import prepare_paper
    from validator.scientific_result_v1.source_tasks.source import selected_cards
    from validator.scientific_result_v1.source_tasks.source_recovery import (
        SourceInventoryDefect,
        corrected_frame,
        validate_defect,
    )
    from validator.scientific_result_v1.source_tasks.store import canonical, sha
    store,packet,ops=fixture(tmp_path,monkeypatch)
    prepared,_=prepare_paper(store=store,packet=packet,parsed_directories={},operations=ops,stages=['week_one'],policy_sha256='1'*64)
    frame=store.load(FileRef.model_validate(prepared['source_frame']),SourceFrame)
    tile=next(t for t in frame.tiles if t.stratum=='PROSE_OR_EQUATION')
    binding=EvaluationBinding(protocol_id=packet.protocol_id,round_id=packet.round_id,stage='week_one',paper_id=packet.paper_id,
        source_packet_sha256=sha(canonical(packet)),source_frame_sha256=sha(canonical(frame)),input_pool_sha256='2'*64,policy_sha256='1'*64,revision=0)
    class Script:
        def __init__(self,role):self.role=role
        def generate(self,*,payload,**kwargs):
            if 'proposal' not in payload:return {'tile_id':tile.tile_id,'outcome':'INVENTORY_DEFECT','cards':[],
                'evidence_requests':[],'unresolved_reason':'Only a non-result introductory statement in this selected tile.'}
            if self.role=='SECONDARY' and uncertain_secondary:
                return dict(request_id=payload['request_id'],proposal_kind='CARD_BATCH',proposal_hash=payload['proposal_hash'],outcome='SOURCE_UNRESOLVED',checked_anchor_ids=[],checked_item_ids=[],issues=[],corrected_proposal=None,evidence_requests=[],unresolved_reason='The independent source arbiter must resolve eligibility')
            return dict(request_id=payload['request_id'],proposal_kind='CARD_BATCH',proposal_hash=payload['proposal_hash'],outcome='ACCEPT',
                checked_anchor_ids=payload['checked_anchor_ids'],checked_item_ids=payload['checked_item_ids'],issues=[],
                corrected_proposal=None,evidence_requests=[],unresolved_reason=None)
    ops.runtimes={role:Script(role) for role in ops.routes}
    with pytest.raises(SourceInventoryDefect) as captured:
        selected_cards(ops,tile,binding,evidence_for(frame,tile.owned_anchor_ids+tile.context_anchor_ids))
    proposal=captured.value.proposal
    changed=corrected_frame(store,packet,frame,proposal)
    assert changed.revision==1 and changed.anchors==frame.anchors and changed.tables==frame.tables
    assert not next(t for t in changed.tiles if t.tile_id==tile.tile_id).eligible_content
    assert validate_docling_frame(store,changed,packet)
    # The original independent parse and source artifact are unchanged.
    assert changed.source_packet_sha256==frame.source_packet_sha256
    proof=json.loads(store.read(proposal));proof['judge_runs']=proof['judge_runs'][:1]
    with pytest.raises(ValueError,match='distinct independent confirmed review'):
        validate_defect(store,store.put(proof),frame)
    forged=changed.model_copy(update={'tiles':[t.model_copy(update={'eligible_content':True}) for t in changed.tiles]})
    with pytest.raises(ValueError,match='differs from retained'):
        validate_docling_frame(store,forged,packet)


def test_judge_keyed_wire_rejects_omissions_wrong_dimension_and_duplicate_keys():
    import json

    from validator.scientific_result_v1.source_tasks.runtime import StrictAdapter
    adapter=StrictAdapter({'properties':{'checks':{'type':'object','properties':{'NUMERIC_FIDELITY':{},'ATTACHMENT':{}}}}})
    valid={'outcome':'ASSESSED','checks':{d:{'dimension':d,'status':'PASS'} for d in ['NUMERIC_FIDELITY','ATTACHMENT']}}
    assert len(adapter.parse(None,json.dumps(valid))['checks'])==2
    valid['checks'].pop('ATTACHMENT')
    with pytest.raises(ValueError,match='every applicable'):adapter.parse(None,json.dumps(valid))
    with pytest.raises(ValueError,match='every applicable'):
        adapter.parse(None,'{"outcome":"ASSESSED","checks":{"NUMERIC_FIDELITY":{"dimension":"ATTACHMENT"},"ATTACHMENT":{"dimension":"ATTACHMENT"}}}')
    with pytest.raises(ValueError,match='Duplicate'):
        adapter.parse(None,'{"outcome":"ASSESSED","checks":{},"checks":{}}')


def test_two_hour_judging_is_explicit_and_does_not_change_legacy_default(tmp_path):
    from validator.scientific_result_v1.source_tasks.configuration import (
        MODELS,
        freeze_settings,
    )
    from validator.scientific_result_v1.source_tasks.runner import make_operations
    snapshots={m:{'id':m,'architecture':{'input_modalities':['text']},'endpoints':[
        {'provider_name':'test','context_length':200000,'supported_parameters':['structured_outputs']}]}
        for m in set(MODELS.values())}
    store=Store(tmp_path)
    old,_=freeze_settings(store,route_snapshots=snapshots,execution_policy={})
    new,_=freeze_settings(store,route_snapshots=snapshots,execution_policy={},sealed_evaluation_timeout_seconds=7200)
    assert old.sealed_evaluation_timeout_seconds==1800 and new.sealed_evaluation_timeout_seconds==7200
    ops=make_operations(store=store,settings=new,policy_sha256='1'*64,trace_dir=tmp_path/'traces',offline=True)
    assert ops.generation_timeout_seconds==7200
    with pytest.raises(ValueError,match='approved'):
        freeze_settings(store,route_snapshots=snapshots,execution_policy={},sealed_evaluation_timeout_seconds=7201)


def test_provider_not_dispatched_is_known_zero_but_timeout_remains_unknown(tmp_path,monkeypatch):
    import json
    import time

    from tests.source_tasks.test_docling_frame import fixture
    from validator.scientific_result_v1.source_tasks.docling_frame import (
        EligibilityResponse,
    )
    store,_,ops=fixture(tmp_path,monkeypatch)
    class Queue:
        def __enter__(self):ops.deadline=time.time()-1
        def __exit__(self,*args):return False
    ops.gate=Queue()
    with pytest.raises(Hold,match='Deadline expired in queue'):
        ops.request('SOURCE_READER','source_eligibility',{'tiles':[{'tile_id':'late'}]},EligibilityResponse)
    run=ops.used[-1];usage=json.loads(store.get_blob(store.head('provider-usage:'+run)))
    assert usage['billing_available'] is True and usage['usage']['cost']==0 and usage['dispatch_outcome']=='not_dispatched'
    ops.deadline=time.time()+100;ops.gate=__import__('threading').BoundedSemaphore(1)
    def timeout(**kwargs):raise TimeoutError('Response unknown after dispatch')
    ops.runtimes['SOURCE_READER'].generate=timeout
    with pytest.raises(Hold):ops.request('SOURCE_READER','source_eligibility',{'tiles':[{'tile_id':'timeout'}]},EligibilityResponse)
    usage=json.loads(store.get_blob(store.head('provider-usage:'+ops.used[-1])))
    assert usage['billing_available'] is False and usage['dispatch_outcome']=='unknown'


def test_recovery_store_isolates_paper_and_clock_heads_but_reuses_exact_provider_requests(tmp_path):
    from validator.scientific_result_v1.source_tasks.recovery import RecoveryStore
    class Backend:
        def _request(self,*args):raise AssertionError('No network required')
    a=RecoveryStore(tmp_path,Backend(),{'run_id':'a'*32});b=RecoveryStore(tmp_path,Backend(),{'run_id':'b'*32})
    assert a._key('paper:["r","week_one","p"]')!=b._key('paper:["r","week_one","p"]')
    assert a._key('generation-clock:g')!=b._key('generation-clock:g')
    assert a._key('model-operation:request-hash')==b._key('model-operation:request-hash')
    assert a._key('provider-usage:request-hash:1')=='provider-usage:request-hash:1'
    assert a.root!=b.root


def test_preparation_and_judging_clocks_are_separate_and_resume_never_extends_either(tmp_path,monkeypatch):
    import time

    from validator.scientific_result_v1.source_tasks.recovery import phase_deadline
    store=Store(tmp_path/'clocks');manifest={'run_id':'a'*32,'configuration_sha256':'b'*64}
    paper={'stage':'week_one','paper_id':'p','input_pool_sha256':'c'*64,'source_packet_sha256':'d'*64}
    monkeypatch.setattr(time,'time',lambda:100)
    source=phase_deadline(store,manifest,paper,'source_preparation',7200)
    monkeypatch.setattr(time,'time',lambda:3700)
    judge=phase_deadline(store,manifest,paper,'scientific_verification',7200)
    assert source==7300 and judge==10900
    monkeypatch.setattr(time,'time',lambda:8000)
    assert phase_deadline(store,manifest,paper,'source_preparation',7200)==source
    assert phase_deadline(store,manifest,paper,'scientific_verification',7200)==judge


@pytest.mark.parametrize('fail_at,reply_lost', [('provider-usage:',False),('judge-run:',False),('judge-run:',True)])
def test_audit_transport_failure_resumes_saved_completion_without_another_provider_call(tmp_path,monkeypatch,fail_at,reply_lost):
    from tests.source_tasks.test_docling_frame import fixture
    from validator.scientific_result_v1.source_tasks.docling_frame import EligibilityResponse
    store,_,ops=fixture(tmp_path,monkeypatch)
    generate=ops.runtimes['SOURCE_READER'].generate;calls=[]
    def counted(**kwargs):calls.append(kwargs);return generate(**kwargs)
    ops.runtimes['SOURCE_READER'].generate=counted
    advance=store.advance;failed=[]
    def flaky(key,parent,child):
        if key.startswith(fail_at) and not failed:
            failed.append(key)
            if reply_lost:advance(key,parent,child)
            raise OSError('Audit transport interrupted')
        return advance(key,parent,child)
    monkeypatch.setattr(store,'advance',flaky)
    data={'tiles':[{'tile_id':'one'}]}
    with pytest.raises(OSError,match='Audit transport'):
        ops.request('SOURCE_READER','source_eligibility',data,EligibilityResponse)
    answer=ops.request('SOURCE_READER','source_eligibility',data,EligibilityResponse)
    assert answer.items[0].eligible is True and len(calls)==1
    with store.connection() as c:
        rows=c.execute('select response,error from attempts').fetchall()
    assert len(rows)==1 and rows[0][0] is not None and rows[0][1] is None
    assert len(set(ops.used))==1


def test_lost_terminal_write_reply_does_not_redispatch_or_change_saved_response(tmp_path,monkeypatch):
    from tests.source_tasks.test_docling_frame import fixture
    from validator.scientific_result_v1.source_tasks.docling_frame import EligibilityResponse
    store,_,ops=fixture(tmp_path,monkeypatch);calls=[]
    generate=ops.runtimes['SOURCE_READER'].generate
    def counted(**kwargs):calls.append(kwargs);return generate(**kwargs)
    ops.runtimes['SOURCE_READER'].generate=counted
    finish=store.finish;failed=[]
    def lost_reply(*args,**kwargs):
        finish(*args,**kwargs)
        if not failed:failed.append(True);raise OSError('Terminal commit reply lost')
    monkeypatch.setattr(store,'finish',lost_reply)
    data={'tiles':[{'tile_id':'one'}]}
    with pytest.raises(OSError,match='Terminal commit reply'):
        ops.request('SOURCE_READER','source_eligibility',data,EligibilityResponse)
    assert ops.request('SOURCE_READER','source_eligibility',data,EligibilityResponse).items[0].eligible
    assert len(calls)==1


def test_response_upload_failure_keeps_paid_response_and_original_usage(tmp_path,monkeypatch):
    import json
    from tests.source_tasks.test_docling_frame import fixture
    from validator.scientific_result_v1.source_tasks.docling_frame import EligibilityResponse
    store,_,ops=fixture(tmp_path,monkeypatch);calls=[];failed=[]
    generate=ops.runtimes['SOURCE_READER'].generate
    def counted(**kwargs):calls.append(kwargs);return generate(**kwargs)
    ops.runtimes['SOURCE_READER'].generate=counted
    put=store.put_blob
    def unavailable(digest,raw):
        if isinstance(json.loads(raw),dict) and 'items' in json.loads(raw) and not failed:
            failed.append(True);raise OSError('Response blob upload interrupted')
        return put(digest,raw)
    monkeypatch.setattr(store,'put_blob',unavailable)
    data={'tiles':[{'tile_id':'one'}]}
    with pytest.raises(OSError,match='Response blob'):
        ops.request('SOURCE_READER','source_eligibility',data,EligibilityResponse)
    assert ops.request('SOURCE_READER','source_eligibility',data,EligibilityResponse).items[0].eligible
    assert len(calls)==1


def test_adjacent_retrieval_moves_beyond_already_supplied_neighbors_and_includes_page_ocr():
    from validator.scientific_result_v1.source_tasks.contracts import PageLocator
    anchors=[SourceAnchor(anchor_id=k,document_uid='d',source_sha256='1'*64,
        locator=PageLocator(kind='page',page_index=2,bbox=(0.,0.,1.,1.)),exact_text=text,rendering=None)
        for k,text in [('previous','Previous context'),('owned','The sentence is split'),('next','Its first continuation'),('complete','Additional readable continuation'),('d1-ocr-page2','The complete original OCR sentence and figure axis label.')]]
    frame=SimpleNamespace(anchors=anchors,tables=[])
    current={'anchors':[a.model_dump(mode='json') for a in anchors[:3]]}
    request=EvidenceRequest(request_id='e',kind='ADJACENT_TEXT',anchor_ids=['owned'],document_uid='d',query='Complete sentence and axis label')
    result=expand_evidence(frame,current,[request],8000,tokenizers={'test':lambda s:len(s)//4})
    ids={a['anchor_id'] for a in result['anchors']}
    assert {'previous','owned','next','complete','d1-ocr-page2'}<=ids
    assert all(a==next(v for v in anchors if v.anchor_id==a['anchor_id']).model_dump(mode='json') for a in result['anchors'])


def test_table_notes_retrieves_unlinked_note_text_and_page_ocr_without_other_pages():
    from validator.scientific_result_v1.source_tasks.contracts import PageLocator,TableLocator
    def page(key,text,p):return SourceAnchor(anchor_id=key,document_uid='d',source_sha256='1'*64,
        locator=PageLocator(kind='page',page_index=p,bbox=(0.,0.,1.,1.)),exact_text=text,rendering=None)
    header=page('caption','Table 4. Treatment options',3)
    row=SourceAnchor(anchor_id='row',document_uid='d',source_sha256='1'*64,locator=TableLocator(kind='table',table_uid='t',first_row=0,stop_row=1,column_names=[]),exact_text='Treatment A* | 20 mg',rendering=None)
    note=page('ordinary-text','* Only under the stated conditions.',3)
    ocr=page('d1-ocr-page3','Treatment A* 20 mg. * Only under the stated conditions.',3)
    unrelated=page('other-page','Unrelated appendix',9)
    table=SimpleNamespace(anchor_ids=['caption','row'],header_anchor_ids=['caption'],note_anchor_ids=[],data_row_anchor_ids=['row'])
    frame=SimpleNamespace(anchors=[header,row,note,ocr,unrelated],tables=[table])
    request=EvidenceRequest(request_id='e',kind='TABLE_NOTES',anchor_ids=['row'],document_uid='d',query='Missing asterisk definition')
    result=expand_evidence(frame,{'anchors':[header.model_dump(mode='json'),row.model_dump(mode='json')]},[request],8000,tokenizers={'test':lambda s:len(s)//4})
    assert {a['anchor_id'] for a in result['anchors']}=={'caption','row','ordinary-text','d1-ocr-page3'}


def test_ten_recovery_reviewers_share_two_memory_heavy_parser_slots(monkeypatch):
    import threading,time
    from concurrent.futures import ThreadPoolExecutor
    from validator.scientific_result_v1.source_tasks.recovery import prepare_recovery_frame
    lock=threading.Lock();active=0;peak=0;completed=[]
    def parse(identity):
        nonlocal active,peak
        with lock:active+=1;peak=max(peak,active)
        time.sleep(.02)
        with lock:active-=1;completed.append(identity)
        return identity
    monkeypatch.setattr('validator.scientific_result_v1.source_tasks.docling_frame.prepare_docling_frame',parse)
    with ThreadPoolExecutor(max_workers=10) as pool:assert list(pool.map(prepare_recovery_frame,range(10)))==list(range(10))
    assert peak==2 and sorted(completed)==list(range(10))
