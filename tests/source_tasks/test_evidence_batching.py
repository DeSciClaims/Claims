"""Whole-source inspection, exact witness unions and unchanged scientific weights."""
# ruff: noqa: F811
import copy,json
import pytest
from types import SimpleNamespace
from tests.source_tasks.test_source_scoring import generated,packet_case  # noqa: F401
from tests.source_tasks.test_docling_frame import fixture
from validator.scientific_result_v1.source_tasks.contracts import (SourceFrame,SourceAnchor,TextLocator,SourceTile,
    RequirementMatch,SourceRequirement,JudgeRunReceipt,EvidenceSearchReceipt,FileRef)
from validator.scientific_result_v1.source_tasks.store import sha,canonical
from validator.scientific_result_v1.source_tasks.packets import Index
from validator.scientific_result_v1.source_tasks.packages import Package
from validator.scientific_result_v1.source_tasks.evidence_batching import prepare,validate_search,source_size
from validator.scientific_result_v1.source_tasks.sharding import partition
from validator.scientific_result_v1.source_tasks.operations import Hold
from validator.scientific_result_v1.source_tasks.budgets import SOURCE_TOKEN_BUDGETS
SOURCE_LIMIT=SOURCE_TOKEN_BUDGETS[0]
SCALE=SOURCE_LIMIT//32000


def case(generated,tmp_path,monkeypatch):
    path,_,_=generated;index=Index(Package(path,'a'));store,source,ops=fixture(tmp_path,monkeypatch,context_window_tokens=262144)
    anchors=[]
    for i in range(8):
        text=('Reported estimate 3.50. ' if i==0 else 'Contradictory adjusted estimate 2.15. ' if i==7 else 'Other source context. ')+('original source material '*(1300*SCALE))
        anchors.append(SourceAnchor(anchor_id='anchor-'+str(i),document_uid='doc',source_sha256=source.documents[0].file.sha256,
            locator=TextLocator(kind='text',start=i*50000,stop=i*50000+len(text)),exact_text=text,rendering=None))
    frame=SourceFrame(protocol_id=source.protocol_id,paper_id=source.paper_id,source_packet_sha256=sha(canonical(source)),parser_config_sha256='1'*64,
        revision=0,state='READY',anchors=anchors,tables=[],tiles=[SourceTile(tile_id='tile',document_uid='doc',stratum='PROSE_OR_EQUATION',
            eligible_content=True,owned_anchor_ids=[a.anchor_id for a in anchors],context_anchor_ids=[],table_uid=None,first_data_row=None,stop_data_row=None)],excluded_region_anchor_ids=[],errors=[])
    target=next(o for o in index.objects if o.kind=='FIELD')
    task,packet=packet_case(index,store,refs=[target.original_ref])
    binding=task.binding.model_copy(update={'source_frame_sha256':sha(canonical(frame)),'source_packet_sha256':sha(canonical(source)),'policy_sha256':ops.policy_hash})
    task=task.model_copy(update={'task_kind':'OUTPUT_AUDIT','tile_ids':[],'output_alias':'a','source_bucket_id':'UNANCHORED','requirements':[],
        'binding':binding,'evidence_anchor_ids':[a.anchor_id for a in anchors]})
    packet=index.packet(task,binding,packet.match,'5'*64,[],owned_refs=[target.original_ref])
    ops.tokenizers={role:lambda s:len(s)//4 for role in ops.routes}
    class Runtime:
        def __init__(self,role):self.role=role
        def generate(self,*,payload,stage,**kwargs):
            assert 'source_evidence_lookup' in stage
            assert not any(k in payload for k in ('primary','secondary','verdict','rationale'))
            desired='anchor-0' if self.role=='PRIMARY' else 'anchor-7'
            return dict(request_id=payload['request_id'],inspected_anchor_ids=payload['inspected_anchor_ids'],inspected_query_ids=payload['inspected_query_ids'],
                witnesses={key:[desired] if desired in payload['inspected_anchor_ids'] else [] for key in payload['inspected_query_ids']})
    ops.runtimes={role:Runtime(role) for role in ops.routes}
    return index,store,ops,frame,task,packet


def runs(store,ops):
    rs={};requests={}
    for ref in ops.run_refs:
        run=store.load(ref,JudgeRunReceipt);rs[run.run_id]=run;requests[run.run_id]=json.loads(store.read(run.request))
    return rs,requests


def test_both_roles_scan_every_batch_and_late_contradiction_is_not_dropped(generated,tmp_path,monkeypatch):
    _,store,ops,frame,task,packet=case(generated,tmp_path,monkeypatch)
    assert source_size(frame,task.evidence_anchor_ids,ops.tokenizers)>SOURCE_LIMIT
    [(child,rendered,evidence,summary)]=prepare(ops,frame,[(task,packet)])
    assert child==task and rendered.objects==packet.objects and rendered.closures==packet.closures
    assert rendered.evidence_anchor_ids==['anchor-0','anchor-7']
    assert [a['anchor_id'] for a in evidence['anchors']]==rendered.evidence_anchor_ids
    assert summary['source_batch_count']>1
    rs,requests=runs(store,ops)
    search=validate_search(store,summary,frame,rendered,task,rs,requests)
    assert len(search.scans)==2*summary['source_batch_count']
    assert all(run['operation']=='source_evidence_lookup' for run in requests.values())
    # Lookup and repeated context create no extra task, verdict or scientific bit.
    assert rendered.task_id==task.task_id and rendered.task_card_sha256==packet.task_card_sha256
    for role in ['PRIMARY','SECONDARY']:
        inspected=[a for s in search.scans if s.role==role for a in s.anchor_ids]
        assert inspected==task.evidence_anchor_ids


@pytest.mark.parametrize('tamper',['missing_batch','drop_contradiction','changed_assertion','forged_complete'])
def test_source_batch_replay_rejects_missing_coverage_or_changed_assertions(generated,tmp_path,monkeypatch,tamper):
    _,store,ops,frame,task,packet=case(generated,tmp_path,monkeypatch)
    [(child,rendered,_,summary)]=prepare(ops,frame,[(task,packet)]);rs,requests=runs(store,ops)
    search=store.load(FileRef.model_validate(summary['receipt']),EvidenceSearchReceipt)
    summary=copy.deepcopy(summary)
    if tamper=='missing_batch':
        search=search.model_copy(update={'scans':search.scans[1:]});summary['receipt']=store.put(search).model_dump(mode='json')
    elif tamper=='drop_contradiction':rendered=rendered.model_copy(update={'evidence_anchor_ids':['anchor-0']})
    elif tamper=='changed_assertion':rendered=rendered.model_copy(update={'objects':[]})
    else:summary['source_anchor_count']=1
    with pytest.raises(ValueError):validate_search(store,summary,frame,rendered,child,rs,requests)


def test_source_only_size_triggers_complete_requirement_shards(generated,tmp_path,monkeypatch):
    index,store,ops,frame,task,packet=case(generated,tmp_path,monkeypatch)
    fields=[o for o in index.objects if o.kind=='FIELD'][:2]
    requirements=[SourceRequirement(requirement_id=str(i),statement='Complete independent finding '+str(i),qualifications=[],
        evidence_anchor_ids=[a.anchor_id for a in frame.anchors[i*4:(i+1)*4]],encoding_constraints=[]) for i in range(2)]
    # Smaller original anchors give two scientifically complete source groups.
    frame=frame.model_copy(update={'anchors':[a.model_copy(update={'exact_text':a.exact_text[:18000*SCALE]}) for a in frame.anchors]})
    task=task.model_copy(update={'task_kind':'CONTENT','requirements':requirements,'tile_ids':['tile'],'output_alias':None,'source_bucket_id':None})
    match=packet.match.model_copy(update={'matches':[RequirementMatch(requirement_id=str(i),output_refs=[f.original_ref]) for i,f in enumerate(fields)],'candidate_refs':[f.original_ref for f in fields]})
    packet=index.packet(task,task.binding,match,'5'*64,[])
    children=partition(index,task,packet,tokenizers=ops.tokenizers,source_frame=frame)
    assert len(children)==2
    assert {r.requirement_id for t,_ in children for r in t.requirements}=={'0','1'}
    assert all(source_size(frame,t.evidence_anchor_ids,ops.tokenizers)<=SOURCE_LIMIT for t,_ in children)
    assert set(children[0][0].evidence_anchor_ids).isdisjoint(children[1][0].evidence_anchor_ids)
    assert all(p.packet_complete and not p.omitted_due_to_limit_refs for _,p in children)


def test_single_joint_requirement_keeps_mandatory_witnesses_even_when_too_large(generated,tmp_path,monkeypatch):
    _,_,ops,frame,task,packet=case(generated,tmp_path,monkeypatch)
    requirement=SourceRequirement(requirement_id='joint',statement='One inseparable estimate with its uncertainty and conditions',qualifications=[],evidence_anchor_ids=task.evidence_anchor_ids,encoding_constraints=[])
    task=task.model_copy(update={'task_kind':'CONTENT','requirements':[requirement],'tile_ids':['tile'],'output_alias':None,'source_bucket_id':None})
    packet=packet.model_copy(update={'task_card_sha256':sha(canonical(task))})
    with pytest.raises(Hold,match='complete joint source witness set'):
        prepare(ops,frame,[(task,packet)])


def test_failed_batch_retains_attempts_and_never_certifies_absence(generated,tmp_path,monkeypatch):
    _,store,ops,frame,task,packet=case(generated,tmp_path,monkeypatch)
    original=ops.runtimes['PRIMARY'].generate
    def invalid(**kwargs):
        response=original(**kwargs)
        response['inspected_anchor_ids']=[]
        return response
    ops.runtimes['PRIMARY'].generate=invalid
    with pytest.raises(Hold):prepare(ops,frame,[(task,packet)])
    rs,_=runs(store,ops)
    assert any(run.error for run in rs.values())
    assert any(run.role=='SECONDARY' and not run.error for run in rs.values())


def test_source_only_archive_shards_repeat_headers_and_notes(generated,tmp_path,monkeypatch):
    from validator.scientific_result_v1.source_tasks.contracts import SourceTable
    index,store,ops,frame,task,_=case(generated,tmp_path,monkeypatch)
    ids=[a.anchor_id for a in frame.anchors[:4]]
    anchors=[a.model_copy(update={'exact_text':a.exact_text[:12000*SCALE] if i in (0,3) else a.exact_text*2}) for i,a in enumerate(frame.anchors[:4])]
    uid=next(iter(index.package.csv))
    table=SourceTable(table_uid='trusted',document_uid='doc',anchor_ids=[ids[0]],numerical=True,
        data_row_count=2,layout_receipt=store.put({'fixture':True}),data_row_anchor_ids=ids[1:3],
        logical_column_names=['outcome','a','b','p'],header_anchor_ids=ids[:1],note_anchor_ids=ids[3:])
    tile=frame.tiles[0].model_copy(update={'stratum':'TABLE','table_uid':'trusted',
        'owned_anchor_ids':ids[1:3],'context_anchor_ids':[ids[0],ids[3]],'first_data_row':0,'stop_data_row':2})
    frame=frame.model_copy(update={'anchors':anchors,'tables':[table],'tiles':[tile]})
    task=task.model_copy(update={'task_kind':'ARCHIVE','tile_ids':['tile'],'evidence_anchor_ids':ids,
        'output_alias':None,'source_bucket_id':None,'requirements':[]})
    match=index.archive_match(task,store,{uid})
    packet=index.packet(task,task.binding,match,'5'*64,[],table_ranges={uid:(0,2)})
    children=partition(index,task,packet,tokenizers=ops.tokenizers,table_ranges={uid:(0,2)},source_frame=frame)
    assert len(children)==2
    assert [set(t.evidence_anchor_ids) for t,_ in children]==[{ids[0],ids[1],ids[3]},{ids[0],ids[2],ids[3]}]
    assert all(source_size(frame,t.evidence_anchor_ids,ops.tokenizers)<=SOURCE_LIMIT for t,_ in children)
