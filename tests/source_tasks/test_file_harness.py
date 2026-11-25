"""Frozen harness switching, native auditing and provider-free workspace replay."""
import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from validator.scientific_result_v1.source_tasks.configuration import MODELS,freeze_settings
from validator.scientific_result_v1.source_tasks.contracts import ProviderParameters,FileRef
from validator.scientific_result_v1.source_tasks.harness import TOOLS
from validator.scientific_result_v1.source_tasks.runner import make_operations
from validator.scientific_result_v1.source_tasks.store import Store,canonical
from validator.scientific_result_v1.source_tasks.file_provider import ProviderAudit
from validator.scientific_result_v1.source_tasks.file_runtime import retain_trace,replay_trace
from validator.scientific_result_v1.source_tasks.file_judge import materialize,WorkspaceTools,_json
from tests.source_tasks.test_file_judge import request


def settings_for(store,monkeypatch,mode='hermes-files'):
    monkeypatch.setattr('validator.scientific_result_v1.source_tasks.harness.hermes_identity',lambda p:{'source_sha256':'a'*64})
    snapshots={m:{'id':m,'architecture':{'input_modalities':['text']},'endpoints':[
        {'provider_name':'default','context_length':262144,'supported_parameters':['structured_outputs','tools','tool_choice']}]}
        for m in set(MODELS.values())}
    return freeze_settings(store,route_snapshots=snapshots,execution_policy={},harness=mode,
                           hermes_python='isolated-python' if mode=='hermes-files' else None)[0]


def test_frozen_harness_reaches_every_role_and_request_and_cannot_be_mixed(tmp_path,monkeypatch):
    store=Store(tmp_path/'store');settings=settings_for(store,monkeypatch)
    ops=make_operations(store=store,settings=settings,policy_sha256='1'*64,trace_dir=tmp_path/'traces',offline=True)
    from validator.scientific_result_v1.source_tasks.file_runtime import FileAgentRuntime
    assert all(isinstance(v,FileAgentRuntime) for v in ops.runtimes.values())
    assert all(r.max_output_tokens==32768 and r.request_timeout_seconds==7200 for r in ops.routes.values())
    assert ops.scoped().harness==ops.harness
    for route in ops.routes.values():
        params=store.load(route.provider_parameters,ProviderParameters)
        assert params.tools==TOOLS and params.tool_choice=='auto'
    from validator.scientific_result_v1.source_tasks.contracts import SourceTaskBatch
    req=ops.prepare_request('PRIMARY','source_card',{},SourceTaskBatch)
    assert req['harness']==ops.harness
    old=copy.deepcopy(settings.model_dump(mode='json'));old['model_routes'][0]['max_output_tokens']=16384
    from validator.scientific_result_v1.source_tasks.contracts import FrozenSettings
    with pytest.raises(ValueError,match='parameters differ'):
        make_operations(store=store,settings=FrozenSettings.model_validate_json(json.dumps(old)),
                         policy_sha256='1'*64,trace_dir=tmp_path/'traces',offline=True)


def test_live_runtime_identity_must_match_without_disclosing_keys(tmp_path,monkeypatch):
    store=Store(tmp_path/'store');settings=settings_for(store,monkeypatch)
    monkeypatch.setenv('CLAIMS_SCIENTIFIC_HERMES_PYTHON','different-python')
    monkeypatch.setattr('validator.scientific_result_v1.source_tasks.harness.hermes_identity',lambda p:{'source_sha256':'b'*64})
    with pytest.raises(ValueError,match='runtime differs'):
        make_operations(store=store,settings=settings,policy_sha256='1'*64,trace_dir=tmp_path/'traces')


def test_file_mode_does_not_apply_monolithic_prompt_caps_to_file_bytes(tmp_path,monkeypatch):
    from validator.scientific_result_v1.source_tasks.contracts import SourceTaskBatch
    from validator.scientific_result_v1.source_tasks.operations import Hold
    store=Store(tmp_path/'store');settings=settings_for(store,monkeypatch)
    ops=make_operations(store=store,settings=settings,policy_sha256='1'*64,trace_dir=tmp_path/'traces',offline=True)
    with pytest.raises(Hold,match='uncached model operation'):
        ops.request('PRIMARY','source_card',{'original_large_file':'science '*100000},SourceTaskBatch,
                     source_tokens=900000,packet_tokens=900000)


def test_native_audit_pins_model_and_tools_without_pinning_providers(tmp_path):
    audit=ProviderAudit(tmp_path,model='model',context_window=100000,max_tokens=32768,api_key='secret')
    request=audit.prepare({'model':'model','messages':[{'role':'user','content':'Original input'}]})
    assert request['extra_body']['provider']=={'require_parameters':True,'allow_fallbacks':True}
    assert request['temperature']==0 and request['max_tokens']==32768
    with pytest.raises(ValueError,match='unfrozen model'):audit.prepare({'model':'other'})
    with pytest.raises(ValueError,match='pinning'):audit.prepare({'model':'model','extra_body':{'provider':{'only':['vendor']}}})
    usage={'prompt_tokens':12,'completion_tokens':3,'cost':0.012}
    response=SimpleNamespace(model_dump=lambda **k:{'id':'gen-test','usage':usage})
    audit.record(request,0,response=response)
    assert audit.summary()['cost']==0.012
    audit.record(request,0,error=ValueError('secret unavailable'))
    assert audit.summary()['cost'] is None and audit.summary()['unknown_billing_calls']==1
    assert 'secret' not in (tmp_path/'provider_calls.jsonl').read_text()


def test_actual_model_context_guard_counts_messages_not_external_files(tmp_path):
    audit=ProviderAudit(tmp_path,model='model',context_window=33000,max_tokens=32768,api_key='secret')
    with pytest.raises(ValueError,match='context capacity'):
        audit.prepare({'model':'model','messages':[{'role':'user','content':'original content '*1000}]})


def test_retained_workspace_replays_and_changed_native_model_is_rejected(tmp_path):
    req=request();req['route']={'model':'test','max_output_tokens':32768};req['harness']={'mode':'hermes-files'}
    root=tmp_path/'work';materialize(root,req);tools=WorkspaceTools(root);tools.read('request.json')
    data=req['payload'];answer={k:data[k] for k in ('request_id','inspected_anchor_ids','inspected_query_ids')}
    answer['witnesses']={q:[] for q in data['inspected_query_ids']}
    assert tools.submit(answer)['accepted']
    event={'model':'test','max_tokens':32768,'temperature':0,'provider':{'require_parameters':True,'allow_fallbacks':True},'tools':[]}
    (root/'provider_calls.jsonl').write_text(json.dumps(event)+'\n')
    store=Store(tmp_path/'store');parsed=store.put(answer)
    proof=retain_trace(store,root,req)
    assert replay_trace(store,proof,req,parsed)['files']>5
    event['model']='unfrozen';(root/'provider_calls.jsonl').write_text(json.dumps(event)+'\n')
    altered=retain_trace(store,root,req)
    with pytest.raises(ValueError,match='native call differs'):replay_trace(store,altered,req,parsed)


def test_replay_checks_raw_science_and_attested_metadata(tmp_path):
    from validator.scientific_result_v1.source_tasks.file_judge import submission_schema
    req=request();req['route']={'model':'test','max_output_tokens':32768,'context_window_tokens':262144}
    req['harness']={'mode':'hermes-files','version':8}
    root=tmp_path/'work';materialize(root,req);tools=WorkspaceTools(root)
    tools.read('source/evidence.json');tools.read('task.json')
    answer={'request_id':req['payload']['request_id'],'witnesses':{q:[] for q in req['payload']['inspected_query_ids']}}
    assert tools.submit(answer)['accepted']
    wire=json.loads((root/'output.json').read_text())
    _json(root/'runtime_context.json',{'context_window_tokens':262144,'reserved_output_tokens':32768,'compression_threshold_tokens':150000})
    event={'model':'test','max_tokens':32768,'temperature':0,'provider':{'require_parameters':True,'allow_fallbacks':True},
           'api_base':'https://openrouter.ai/api/v1','tool_choice':'auto',
           'tools':[{'type':'function','function':{'name':'claims_submit_output','parameters':submission_schema(req['stage_schema'],req['operation'])}}]}
    (root/'provider_calls.jsonl').write_text(json.dumps(event)+'\n')
    store=Store(tmp_path/'store');parsed=store.put(wire)
    assert replay_trace(store,retain_trace(store,root,req),req,parsed)['files']>5
    answer['witnesses']['query-0']=['invented'];_json(root/'model_submission.json',answer)
    with pytest.raises(ValueError,match='metadata changed'):
        replay_trace(store,retain_trace(store,root,req),req,parsed)
