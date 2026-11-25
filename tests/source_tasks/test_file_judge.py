"""Check scientific bytes, tool confinement and validated artifact boundaries."""
import copy
import json
from pathlib import Path

import pytest

from validator.scientific_result_v1.source_tasks.file_judge import materialize, WorkspaceTools, FileJudge
from validator.scientific_result_v1.source_tasks.response_schema import response_schema
from validator.scientific_result_v1.source_tasks.contracts import EvidenceBatchResponse
from tests.source_tasks.test_request_sizing import lookup
from tests.source_tasks.test_source_scoring import generated, packet_case  # noqa: F401


def request():
    data = lookup(3, 2)
    return {'instructions':'Inspect actual source footnotes.', 'operation':'source_evidence_lookup',
            'payload':data, 'stage_schema':response_schema(EvidenceBatchResponse, data)}


def test_workspace_preserves_original_source_and_exact_request(tmp_path):
    req=request(); before=copy.deepcopy(req)
    materialize(tmp_path/'work', req)
    root=tmp_path/'work'
    assert json.loads((root/'request.json').read_text())==before==req
    catalog=json.loads((root/'catalog.json').read_text())
    assert [json.loads((root/x['file']).read_text()) for x in catalog['source']]==req['payload']['authoritative_evidence']['anchors']
    assert 'stage_schema' not in json.loads((root/'task.json').read_text())


def test_read_pagination_retains_every_character_and_search_has_no_absence_certificate(tmp_path):
    materialize(tmp_path/'work', request()); tools=WorkspaceTools(tmp_path/'work')
    path='source/anchors/000001.json'; offset=0; parts=[]
    while True:
        result=tools.read(path, offset, 13); parts.append(result['text'])
        if result['next_offset'] is None:break
        offset=result['next_offset']
    assert ''.join(parts)==(tmp_path/'work'/path).read_text()
    result=tools.search('±', 'source/anchors/', limit=1)
    assert result['total_hits']==3 and result['next_offset']==1 and result['absence_certificate'] is False
    assert len(tools.search('±', 'source/anchors/', offset=1)['hits'])==2


@pytest.mark.parametrize('path',['../secret','/etc/passwd','output.json','hermes-home/config.yaml'])
def test_tools_cannot_read_outside_retained_inputs(tmp_path,path):
    materialize(tmp_path/'work',request()); tools=WorkspaceTools(tmp_path/'work')
    with pytest.raises(ValueError):tools.read(path)


def test_detects_input_mutation_and_symlink_escape(tmp_path):
    materialize(tmp_path/'work',request()); tools=WorkspaceTools(tmp_path/'work')
    path=tmp_path/'work'/'task.json'; path.write_text('{}')
    with pytest.raises(ValueError,match='changed'):tools.read('task.json')
    path.unlink(); path.symlink_to(tmp_path/'outside')
    with pytest.raises(ValueError):tools.read('task.json')


def test_submit_rejects_unknown_ids_without_writing_or_repairing(tmp_path):
    req=request(); materialize(tmp_path/'work',req); tools=WorkspaceTools(tmp_path/'work')
    data=req['payload']
    answer={k:data[k] for k in ['request_id','inspected_anchor_ids','inspected_query_ids']}
    answer['witnesses']={q:[] for q in data['inspected_query_ids']}
    wrong=copy.deepcopy(answer); wrong['witnesses']['query-0']=['invented']
    assert not tools.submit(wrong)['accepted']
    assert not (tmp_path/'work'/'output.json').exists()
    tools.read('source/evidence.json')
    assert tools.submit(answer)['accepted']
    assert json.loads((tmp_path/'work'/'output.json').read_text())==answer


def test_models_are_restricted_and_no_production_runtime_is_changed():
    with pytest.raises(ValueError,match='Unapproved'):FileJudge(python='python',model='unapproved')
    judge=FileJudge(python='python',model='deepseek/deepseek-v4-flash-0731')
    assert judge.max_tokens==32768
    from validator.scientific_result_v1.source_tasks.runtime import SourceTaskRuntime
    assert SourceTaskRuntime(model='deepseek/deepseek-v4-flash-0731',api_key='unused').max_tokens==16384


def test_compact_packet_is_decoded_with_unchanged_literals_and_edges(generated):
    from validator.scientific_result_v1.source_tasks.packets import Index
    from validator.scientific_result_v1.source_tasks.packages import Package
    from validator.scientific_result_v1.source_tasks.store import Store
    from validator.scientific_result_v1.source_tasks.transport import judge_packet
    path,_,_=generated
    store=Store(path.parent/'files-store'); task,packet=packet_case(Index(Package(path,'a')),store)
    req=request(); req['payload']['packet']=judge_packet(packet)
    root=path.parent/'files-work'; materialize(root,req)
    assert json.loads((root/'candidate/packet.json').read_text())==packet.model_dump(mode='json')
    catalog=json.loads((root/'catalog.json').read_text())
    objects=[json.loads((root/x['file']).read_text()) for x in catalog['candidate'] if x['kind']=='objects']
    assert objects==[o.model_dump(mode='json') for o in packet.objects]


def _fake_process(monkeypatch, mutate=False):
    import subprocess
    seen={}
    class Process:
        def __init__(self,command,**kwargs):
            seen.update(kwargs);seen['command']=command
            root=Path(kwargs['cwd']); req=json.loads((root/'request.json').read_text());data=req['payload']
            answer={k:data[k] for k in ['request_id','inspected_anchor_ids','inspected_query_ids']}
            answer['witnesses']={q:[] for q in data['inspected_query_ids']}
            tools=WorkspaceTools(root);tools.read('request.json')
            assert tools.submit(answer)['accepted']
            if mutate:(root/'instructions.txt').write_text('tampered')
        def wait(self,timeout=None):return 0
    monkeypatch.setattr(subprocess,'Popen',Process)
    return seen


def test_process_uses_fresh_profile_and_revalidates_exact_output(tmp_path,monkeypatch):
    monkeypatch.setenv('HERMES_HOME','/unrelated/profile')
    monkeypatch.setenv('HERMES_PROVIDER_ROUTING','pinned-provider')
    seen=_fake_process(monkeypatch)
    result,root=FileJudge(python='hermes-python',model='minimax/minimax-m3.1-flash').run(request(),tmp_path)
    assert result['witnesses']=={'query-0':[],'query-1':[]}
    assert seen['env']['HERMES_HOME']==str(root/'hermes-home')
    assert 'HERMES_PROVIDER_ROUTING' not in seen['env']
    assert json.loads((root/'hermes-home/config.yaml').read_text())['provider_routing']=={}
    assert seen['start_new_session'] is True
    manifest=json.loads((root/'result_manifest.json').read_text())
    assert manifest['modified_inputs']==[] and manifest['no_scores_or_weights_written'] is True


def test_process_rejects_mutated_source_even_with_valid_response(tmp_path,monkeypatch):
    _fake_process(monkeypatch,mutate=True)
    with pytest.raises(ValueError,match='modified immutable'):
        FileJudge(python='hermes-python',model='minimax/minimax-m3.1-flash').run(request(),tmp_path)


def test_parent_semantic_validator_can_reject_schema_valid_output(tmp_path,monkeypatch):
    _fake_process(monkeypatch)
    def invalid(result):raise ValueError('Scientific coverage was not established')
    with pytest.raises(ValueError,match='coverage was not established'):
        FileJudge(python='hermes-python',model='minimax/minimax-m3.1-flash').run(request(),tmp_path,validate=invalid)


def test_access_audit_counts_complete_reads_not_search_hits_or_echoes(tmp_path):
    from validator.scientific_result_v1.source_tasks.file_judge_audit import access_audit
    root=tmp_path/'work';materialize(root,request());tools=WorkspaceTools(root)
    tools.search('Reported estimate',prefix='source/anchors/')
    assert access_audit(root)['fully_read_source_anchors']==0
    tools.read('source/anchors/000001.json')
    assert access_audit(root)['fully_read_source_anchors']==1
    tools.read('source/evidence.json',0,20)
    assert access_audit(root)['fully_read_source_anchors']==1
    tools.read('source/evidence.json',20,24000)
    audit=access_audit(root)
    assert audit['fully_read_source_anchors']==3 and not audit['unread_source_anchor_ids']
    assert audit['proves_scientific_understanding'] is False


def test_lookup_submission_cannot_forge_inspection_or_repeat_witnesses(tmp_path):
    req=request();root=tmp_path/'work';materialize(root,req);tools=WorkspaceTools(root)
    data=req['payload'];answer={k:data[k] for k in ['request_id','inspected_anchor_ids','inspected_query_ids']}
    answer['witnesses']={q:[] for q in data['inspected_query_ids']}
    assert 'unread source anchors' in tools.submit(answer)['error']
    tools.read('source/evidence.json')
    answer['witnesses']['query-0']=['d1-text0-s0','d1-text0-s0']
    assert 'Duplicate' in tools.submit(answer)['error']
    assert not (root/'output.json').exists()


def test_search_offsets_refer_to_original_unicode_text(tmp_path):
    req=request();req['payload']['authoritative_evidence']['anchors'][0]['exact_text']='Straße: ORIGINAL estimate'
    root=tmp_path/'work';materialize(root,req);tools=WorkspaceTools(root)
    result=tools.search('ORIGINAL',prefix='source/anchors/000000')
    hit=result['hits'][0];text=(root/hit['file']).read_text()
    assert text[hit['character_offset']:hit['character_offset']+8]=='ORIGINAL'


def test_attested_metadata_needs_complete_reads_and_never_repairs_science(tmp_path):
    from validator.scientific_result_v1.source_tasks.file_judge import submission_schema
    req=request();req['harness']={'version':8};root=tmp_path/'attested'
    materialize(root,req);tools=WorkspaceTools(root);data=req['payload']
    answer={'request_id':data['request_id'],'witnesses':{q:[] for q in data['inspected_query_ids']}}
    native=submission_schema(req['stage_schema'],req['operation'])
    assert 'inspected_anchor_ids' not in native['properties']
    assert 'inspected_anchor_ids' in req['stage_schema']['properties']
    assert not tools.submit(answer)['accepted']
    tools.read('source/evidence.json')
    assert 'task.json' in tools.submit(answer)['error']
    tools.read('task.json')
    wrong=copy.deepcopy(answer);wrong['witnesses']['query-0']=['invented']
    assert not tools.submit(wrong)['accepted']
    wrong=copy.deepcopy(answer);wrong['inspected_anchor_ids']=['invented']
    assert not tools.submit(wrong)['accepted']
    assert tools.submit(answer)['accepted']
    output=json.loads((root/'output.json').read_text())
    assert output['witnesses']==answer['witnesses']
    assert output['inspected_anchor_ids']==data['inspected_anchor_ids']
    assert json.loads((root/'model_submission.json').read_text())==answer
    assert json.loads((root/'submission_receipt.json').read_text())=={'controller_fields':{
        k:data[k] for k in ('inspected_anchor_ids','inspected_query_ids')}}
