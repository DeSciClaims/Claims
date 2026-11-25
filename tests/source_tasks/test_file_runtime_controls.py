import json
from collections import Counter
from types import SimpleNamespace
import pytest

from validator.scientific_result_v1.source_tasks.file_judge import (
    profile_config,submit_final_json,materialize,WorkspaceTools,
)
from validator.scientific_result_v1.source_tasks.store import canonical,Store
from tests.source_tasks.test_file_judge import request
from tests.source_tasks.test_source_scoring import generated,packet_case  # noqa: F401


def test_native_schema_keeps_response_structure_and_local_validator_keeps_all_id_constraints():
    from validator.scientific_result_v1.source_tasks.file_judge import submission_schema
    import jsonschema
    ids=['id-'+str(i) for i in range(100)]
    original={'type':'object','required':['ids','state'],'additionalProperties':False,
              'properties':{'ids':{'type':'array','items':{'type':'string','enum':ids},'const':ids},
                            'state':{'type':'string','enum':['PASS','FAIL']}}}
    shaped=submission_schema(original)
    assert shaped['required']==original['required'] and shaped['properties']['state']==original['properties']['state']
    assert shaped['properties']['ids']['minItems']==100 and 'const' not in shaped['properties']['ids']
    assert original['properties']['ids']['const']==ids
    wrong={'ids':['invented']*100,'state':'PASS'}
    jsonschema.validate(wrong,shaped)
    with pytest.raises(jsonschema.ValidationError):jsonschema.validate(wrong,original)


def test_completion_feedback_reuses_history_and_shared_agent_budget(tmp_path):
    from validator.scientific_result_v1.source_tasks.hermes_file_worker import run_artifact_agent
    req=request();root=tmp_path/'work';materialize(root,req);tools=WorkspaceTools(root);tools.read('source/evidence.json')
    data=req['payload'];answer={k:data[k] for k in ['request_id','inspected_anchor_ids','inspected_query_ids']}
    answer['witnesses']={q:[] for q in data['inspected_query_ids']}
    class Agent:
        def __init__(self):self.calls=[];self.max_iterations=8;self.iteration_budget=SimpleNamespace(remaining=8)
        def run_conversation(self,prompt,conversation_history=None):
            self.calls.append((prompt,conversation_history));self.iteration_budget=SimpleNamespace(remaining=self.max_iterations-1)
            return {'final_response':'Let me inspect further.' if len(self.calls)==1 else json.dumps(answer),
                    'messages':[{'role':'assistant','content':'Original retained observations'}],'failed':False,'api_calls':1}
    agent=Agent();result,rounds=run_artifact_agent(agent,'Inspect source',tools)
    assert len(rounds)==2 and agent.iteration_budget.remaining==6
    assert agent.calls[1][1]==rounds[0]['messages'] and 'artifact is still missing' in agent.calls[1][0]
    assert json.loads((root/'output.json').read_text())==answer


def test_artifact_completion_stops_only_after_validation_accepts():
    from validator.scientific_result_v1.source_tasks.hermes_file_worker import execute_tool
    stopped=[]
    rejected=json.loads(execute_tool(lambda **kw:{'accepted':False},{},lambda:stopped.append(True)))
    assert not rejected['accepted'] and not stopped
    accepted=json.loads(execute_tool(lambda **kw:{'accepted':True},{},lambda:stopped.append(True)))
    assert accepted['accepted'] and stopped==[True]


def test_native_tool_selection_is_portable_and_compaction_can_return_text(tmp_path):
    from validator.scientific_result_v1.source_tasks.file_provider import ProviderAudit
    audit=ProviderAudit(tmp_path,model='model',context_window=262144,max_tokens=32768,api_key='test')
    base={'model':'model','messages':[{'role':'user','content':'Inspect retained evidence'}]}
    assert audit.prepare({**base,'tools':[{'type':'function','function':{'name':'claims_read_file'}}]})['tool_choice']=='auto'
    assert audit.prepare(base).get('tool_choice') is None


def test_native_main_and_compaction_calls_cannot_change_endpoint_or_credential(tmp_path):
    from validator.scientific_result_v1.source_tasks.file_provider import ProviderAudit
    audit=ProviderAudit(tmp_path,model='model',context_window=262144,max_tokens=32768,api_key='test-secret')
    def client(base,key):return SimpleNamespace(_client=SimpleNamespace(base_url=base,api_key=key))
    allowed=client('https://openrouter.ai/api/v1/','test-secret');audit.verify_client(allowed)
    assert allowed._client.max_retries==0
    with pytest.raises(ValueError,match='endpoint or credential'):
        audit.verify_client(client('https://other.example/api','test-secret'))
    with pytest.raises(ValueError,match='endpoint or credential'):
        audit.verify_client(client('https://openrouter.ai/api/v1','other-key'))


def test_hermes_profile_uses_frozen_context_for_main_and_compaction_without_provider_pinning():
    config=profile_config('approved-model',262144)
    assert config['model']['context_length']==262144
    assert config['auxiliary']['compression']=={'model':'approved-model','context_length':262144}
    assert config['provider_routing']=={}
    assert config['compression']['abort_on_summary_failure']


def test_final_json_has_same_schema_and_read_proof_guards_as_tool_submission(tmp_path):
    req=request();root=tmp_path/'work';materialize(root,req);tools=WorkspaceTools(root)
    data=req['payload'];answer={k:data[k] for k in ['request_id','inspected_anchor_ids','inspected_query_ids']}
    answer['witnesses']={q:[] for q in data['inspected_query_ids']}
    result={'final_response':json.dumps(answer),'failed':False}
    assert not submit_final_json(result,tools)
    assert not (root/'output.json').exists()
    tools.read('source/evidence.json')
    assert not submit_final_json({'final_response':'I checked everything.'},tools)
    assert not submit_final_json({**result,'failed':True},tools)
    assert submit_final_json(result,tools)
    assert json.loads((root/'output.json').read_text())==answer


def test_file_audit_groups_keep_every_owner_exactly_once_without_cutting_records(generated):
    from validator.scientific_result_v1.source_tasks.packets import Index
    from validator.scientific_result_v1.source_tasks.packages import Package
    from validator.scientific_result_v1.source_tasks.sharding import partition,validate_child
    path,doc,_=generated;index=Index(Package(path,'a'));store=Store(path.parent/'file-audit-groups')
    owned=[o.original_ref for o in index.objects]+[e.original_ref for e in index.relations]
    task,_=packet_case(index,store)
    task=task.model_copy(update={'task_kind':'OUTPUT_AUDIT','output_alias':'a','source_bucket_id':'bucket','requirements':[]})
    match=index.audit_match(task,store,owned)
    packet=index.packet(task,task.binding,match,'5'*64,[],owned_refs=owned)
    assert len(partition(index,task,packet,tokenizers={'small':lambda s:1},owned_refs=owned))==1
    shards=partition(index,task,packet,tokenizers={'large':lambda s:10**9},owned_refs=owned,
                     force_audit_groups=True,unbounded_files=True)
    assert len(shards)>1
    assert Counter(canonical(r) for _,p in shards for r in p.match.candidate_refs)==Counter(map(canonical,owned))
    for child,rendered in shards:validate_child(child,task)
    for i,result in enumerate(doc['results']):
        refs={canonical(o.original_ref) for o in index.objects if o.original_ref.pointer=='/results/'+str(i)
              or o.original_ref.pointer.startswith('/results/'+str(i)+'/fields/')}
        assert any(refs<={canonical(r) for r in p.match.candidate_refs} for _,p in shards)


def test_three_identical_validation_errors_request_fresh_context_without_acceptance(tmp_path):
    from validator.scientific_result_v1.source_tasks.hermes_file_worker import execute_tool,run_artifact_agent
    req=request();req['harness']={'version':9};root=tmp_path/'work';materialize(root,req);tools=WorkspaceTools(root)
    tools.read('task.json');tools.read('source/evidence.json');bad={'request_id':'wrong','witnesses':{}}
    stopped=[]
    for ordinal in range(3):
        response=json.loads(execute_tool(tools.submit,{'output':bad},lambda:stopped.append(True)))
        assert not response['accepted']
        assert bool(response.get('retry_fresh_context'))==(ordinal==2)
    assert stopped==[True] and tools.stalled and not (root/'output.json').exists()
    class Agent:
        max_iterations=80;iteration_budget=SimpleNamespace(remaining=79)
        def run_conversation(self,*a,**kw):return {'api_calls':1,'messages':[],'final_response':'No artifact'}
    _,rounds=run_artifact_agent(Agent(),'Inspect',tools)
    assert len(rounds)==1  # Do not spend another completion round in the stuck context.
    assert not tools.submit({})['accepted'] and not (root/'output.json').exists()
    fresh=WorkspaceTools(root);data=req['payload']
    valid={'request_id':data['request_id'],'witnesses':{q:[] for q in data['inspected_query_ids']}}
    assert fresh.submit(valid)['accepted'] and not fresh.stalled


def test_historical_harness_does_not_acquire_new_recovery_behavior(tmp_path):
    req=request();req['harness']={'version':8};root=tmp_path/'work';materialize(root,req);tools=WorkspaceTools(root)
    for _ in range(4):assert not tools.submit({})['accepted']
    assert not tools.stalled
