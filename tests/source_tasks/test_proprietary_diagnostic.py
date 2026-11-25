import json
from types import SimpleNamespace
import pytest
from validator.scientific_result_v1.source_tasks.proprietary_diagnostic import initialize_budget,policy
from validator.scientific_result_v1.source_tasks.file_judge import FileJudge
from validator.scientific_result_v1.source_tasks.file_provider import ProviderAudit


def setup(tmp_path,cap=2):
    p=tmp_path/'budget.sqlite3'
    budget=initialize_budget(p,[{'id':'openai/gpt-5.5','pricing':{'prompt':'0.000005','completion':'0.00003'}}],cap)
    return budget,policy('openai/gpt-5.5',context_window_tokens=1050000,budget_database=p)


def test_budget_preserves_unknown_reservations_and_rejects_overspend(tmp_path):
    budget,_=setup(tmp_path)
    first=budget.reserve('openai/gpt-5.5',1000,32768);budget.settle(first,0.1,'one')
    second=budget.reserve('openai/gpt-5.5',1000,32768);budget.settle(second,None)
    assert budget.summary()['confirmed_usd']==0.1
    assert budget.summary()['unknown_billing_calls']==1
    with pytest.raises(ValueError,match='spend cap'):budget.reserve('openai/gpt-5.5',1000,32768)


def test_production_allowlist_stays_closed_and_diagnostic_must_be_explicit(tmp_path):
    _,p=setup(tmp_path)
    with pytest.raises(ValueError,match='Unapproved'):FileJudge(python='python',model='openai/gpt-5.5')
    judge=FileJudge(python='python',model='openai/gpt-5.5',diagnostic_policy=p)
    with pytest.raises(ValueError,match='policy differs'):judge.run({'route':{'model':'openai/gpt-5.5'}},tmp_path)


def test_both_harnesses_have_explicit_gpt_parameters_and_native_budget_accounting(tmp_path):
    budget,p=setup(tmp_path)
    audit=ProviderAudit(tmp_path,model=p['model'],context_window=p['context_window_tokens'],max_tokens=32768,
                        api_key='test',diagnostic_policy=p)
    prepared=audit.prepare({'model':p['model'],'messages':[{'role':'user','content':'source'}],
                            'temperature':0,'top_p':1,'n':1,'max_completion_tokens':32768})
    assert not {'temperature','top_p','n','max_completion_tokens'}&set(prepared)
    assert prepared['extra_body']=={'provider':{'require_parameters':True,'allow_fallbacks':True},'reasoning':{'effort':'medium','exclude':True}}
    response=SimpleNamespace(model_dump=lambda **kw:{'id':'response','usage':{'cost':0.01,'prompt_tokens':10,'completion_tokens':5}})
    audit.record(prepared,0,response=response)
    assert budget.summary()['confirmed_usd']==0.01 and budget.summary()['unknown_billing_calls']==0
    assert json.loads((tmp_path/'provider_calls.jsonl').read_text())['temperature'] is None


def test_provider_schema_moves_root_union_and_retains_local_contradiction_check():
    import jsonschema
    from validator.scientific_result_v1.source_tasks.proprietary_diagnostic import provider_schema
    original={'type':'object','properties':{'outcome':{'enum':['ACCEPT','REVISE']},'correction':{'type':['object','null']}},
              'required':['outcome','correction'],'additionalProperties':False,
              'anyOf':[{'properties':{'outcome':{'const':'ACCEPT'},'correction':{'type':'null'}}},
                       {'properties':{'outcome':{'const':'REVISE'},'correction':{'type':'object'}}}]}
    wire=provider_schema(original)
    assert 'anyOf' not in wire and wire['properties']['result']['anyOf']
    jsonschema.validate({'result':{'outcome':'ACCEPT','correction':None}},wire)
    with pytest.raises(jsonschema.ValidationError):jsonschema.validate({'outcome':'ACCEPT','correction':{}},original)


def test_proprietary_full_paper_profiles_are_local_only_and_preserve_default_routes(tmp_path,monkeypatch):
    from validator.scientific_result_v1.source_tasks.configuration import freeze_settings
    from validator.scientific_result_v1.source_tasks.runner import make_operations,APPROVED_MODELS
    from validator.scientific_result_v1.source_tasks.store import Store
    from validator.scientific_result_v1.source_tasks.contracts import SourceReviewResponse
    monkeypatch.setattr('validator.scientific_result_v1.source_tasks.harness.hermes_identity',lambda p:{'source_sha256':'a'*64})
    models={'SOURCE_READER':'openai/gpt-5.5','PRIMARY':'openai/gpt-5.5','SECONDARY':'openai/gpt-5.4','ARBITER':'openai/gpt-5.2'}
    profiles={role:policy(model,context_window_tokens=400000,budget_database=tmp_path/'budget.sqlite3') for role,model in models.items()}
    snapshots={m:{'id':m,'architecture':{'input_modalities':['text']},'endpoints':[{'provider_name':'automatic','context_length':400000,
        'supported_parameters':['structured_outputs','tools','tool_choice']}]} for m in set(models.values())}
    store=Store(tmp_path/'store')
    with pytest.raises(ValueError,match='local-only'):freeze_settings(store,route_snapshots=snapshots,models=models,
        execution_policy={'mode':'competition'},diagnostic_profiles=profiles)
    for harness in ['dspy','hermes-files']:
        settings,_=freeze_settings(store,route_snapshots=snapshots,models=models,harness=harness,hermes_python='fake-python',
            execution_policy={'mode':'local_proprietary_comparison_v1'},diagnostic_profiles=profiles)
        assert all(r.temperature is None and r.top_p is None and r.n is None and r.max_output_tokens==32768 for r in settings.model_routes)
        ops=make_operations(store=store,settings=settings,policy_sha256='b'*64,trace_dir=tmp_path/harness,offline=True)
        assert ops.scoped().diagnostic_profiles==profiles
        assert ops.prepare_request('SECONDARY','source_card_review',{'checked_anchor_ids':[],'checked_item_ids':[]},SourceReviewResponse)['diagnostic_model_policy']==profiles['SECONDARY']
    assert not set(models.values())&APPROVED_MODELS


def test_complete_outcome_branches_keep_empty_checks_and_strict_scoring_checks_valid():
    import copy,jsonschema
    from validator.scientific_result_v1.source_tasks.proprietary_diagnostic import provider_schema
    base={'type':'object','properties':{'outcome':{'type':'string','enum':['ASSESSED','NEED_EVIDENCE']},
        'checks':{'type':'object','properties':{'VALUE':{'type':'boolean'}},'additionalProperties':False}},
        'required':['outcome','checks'],'additionalProperties':False}
    assessed=copy.deepcopy(base);assessed['properties']['outcome']={'const':'ASSESSED','type':'string'}
    assessed['properties']['checks']['required']=['VALUE']
    needs=copy.deepcopy(base);needs['properties']['outcome']={'const':'NEED_EVIDENCE','type':'string'}
    needs['properties']['checks']={'type':'object','properties':{},'additionalProperties':False}
    original={**base,'anyOf':[assessed,needs]};wire=provider_schema(original)
    for answer in [{'outcome':'ASSESSED','checks':{'VALUE':True}},{'outcome':'NEED_EVIDENCE','checks':{}}]:
        jsonschema.validate(answer,original);jsonschema.validate({'result':answer},wire)
    for answer in [{'outcome':'ASSESSED','checks':{}},{'outcome':'NEED_EVIDENCE','checks':{'VALUE':True}}]:
        with pytest.raises(jsonschema.ValidationError):jsonschema.validate(answer,original)
        with pytest.raises(jsonschema.ValidationError):jsonschema.validate({'result':answer},wire)
    def check(node):
        if isinstance(node,dict):
            if node.get('type')=='object':assert set(node.get('properties',{}))==set(node.get('required',[]))
            for v in node.values():check(v)
        elif isinstance(node,list):
            for v in node:check(v)
    check(wire)


def test_large_reference_pools_remain_exact_in_local_validator():
    import jsonschema
    from validator.scientific_result_v1.source_tasks.proprietary_diagnostic import provider_schema
    pointers=['/results/'+str(i)+'/fields/scientific_quantity/value' for i in range(795)]
    original={'type':'object','properties':{'pointer':{'type':'string','enum':pointers}},'required':['pointer'],'additionalProperties':False}
    wire=provider_schema(original)
    assert 'enum' not in wire['properties']['result']['properties']['pointer']
    # Provider grammar is intentionally permissive for these large identity
    # pools; the original mandatory validator still rejects unknown witnesses.
    jsonschema.validate({'result':{'pointer':'/invented'}},wire)
    with pytest.raises(jsonschema.ValidationError):jsonschema.validate({'pointer':'/invented'},original)
    jsonschema.validate({'pointer':pointers[0]},original)


def test_unused_definitions_are_removed_without_changing_reachable_constraints():
    from validator.scientific_result_v1.source_tasks.proprietary_diagnostic import provider_schema
    original={'type':'object','properties':{'id':{'$ref':'#/$defs/Used'}},'required':['id'],'additionalProperties':False,
              '$defs':{'Used':{'type':'string','enum':['known']},'Unused':{'type':'string','enum':['unrelated']}}}
    wire=provider_schema(original)
    assert set(wire['$defs'])=={'Used'} and wire['$defs']['Used']['enum']==['known']


def test_larger_output_allowance_is_explicit_and_bound_to_frozen_endpoint_capacity(tmp_path):
    from validator.scientific_result_v1.source_tasks.configuration import freeze_settings
    from validator.scientific_result_v1.source_tasks.runner import make_operations
    from validator.scientific_result_v1.source_tasks.store import Store
    from validator.scientific_result_v1.source_tasks.proprietary_diagnostic import validate
    models={'SOURCE_READER':'openai/gpt-5.5','PRIMARY':'openai/gpt-5.5',
            'SECONDARY':'openai/gpt-5.4','ARBITER':'openai/gpt-5.2'}
    profiles={role:policy(model,context_window_tokens=400000,budget_database=tmp_path/'budget.sqlite3',
                         unlimited=True,max_output_tokens=65536) for role,model in models.items()}
    snapshots={m:{'id':m,'architecture':{'input_modalities':['text']},
        'endpoints':[{'provider_name':'automatic','context_length':400000,'max_completion_tokens':128000,
                      'supported_parameters':['structured_outputs']}]} for m in set(models.values())}
    store=Store(tmp_path/'store')
    settings,_=freeze_settings(store,route_snapshots=snapshots,models=models,diagnostic_profiles=profiles,
        execution_policy={'mode':'local_proprietary_comparison_v1'})
    ops=make_operations(store=store,settings=settings,policy_sha256='b'*64,trace_dir=tmp_path/'traces',offline=True)
    assert {r.max_output_tokens for r in settings.model_routes}=={65536}
    assert {r.max_tokens for r in ops.runtimes.values()}=={65536}
    with pytest.raises(ValueError,match='Invalid'):
        validate({**profiles['PRIMARY'],'max_output_tokens':True},models['PRIMARY'])
    for capacity in (32768,None,True):
        snapshots[models['PRIMARY']]['endpoints'][0]['max_completion_tokens']=capacity
        with pytest.raises(ValueError,match='endpoint capacity'):
            freeze_settings(store,route_snapshots=snapshots,models=models,diagnostic_profiles=profiles,
                execution_policy={'mode':'local_proprietary_comparison_v1'})
    with pytest.raises(ValueError,match='Invalid'):
        policy(models['PRIMARY'],context_window_tokens=400000,budget_database=tmp_path/'budget.sqlite3',max_output_tokens=65536)


def test_truncated_gpt_response_retains_text_usage_and_billing_without_temperature(tmp_path,monkeypatch):
    import dspy
    from validator.scientific_result_v1.runtime import TruncatedStageError
    from validator.scientific_result_v1.source_tasks.proprietary_diagnostic import dspy_runtime
    budget,_=setup(tmp_path,cap=None)
    profile=policy('openai/gpt-5.5',context_window_tokens=1050000,budget_database=tmp_path/'budget.sqlite3',
                   unlimited=True,max_output_tokens=65536)
    response={'id':'paid-truncated-response','model':profile['model'],
              'choices':[{'finish_reason':'length','message':{'content':'{"result":'}}],
              'usage':{'cost':0.25,'prompt_tokens':100,'completion_tokens':65536}}
    async def fake_forward(self,**kwargs):
        assert 'temperature' not in self.kwargs
        self._check_truncation(response)
        return response
    monkeypatch.setattr(dspy.LM,'aforward',fake_forward)
    runtime=dspy_runtime(diagnostic_policy=profile,model=profile['model'],api_key='test')
    schema={'type':'object','properties':{'ok':{'type':'boolean'}},'required':['ok'],'additionalProperties':False}
    with pytest.raises(TruncatedStageError,match='65536'):
        runtime.generate(instructions='Return the required object.',payload={'stage_schema':schema},
                         output_dir=tmp_path/'traces'/'PRIMARY',stage='source_match')
    complete=[v for v in runtime.usage if v['event']=='request_complete']
    assert len(complete)==1 and complete[0]['provider_request_id']==response['id']
    assert complete[0]['finish_reason']=='length'
    assert next((tmp_path/'traces'/'PRIMARY').glob('*.response.txt')).read_text()=='{"result":'
    assert json.loads(next((tmp_path/'traces'/'PRIMARY').glob('*.response.json')).read_text())['id']==response['id']
    assert budget.summary()['confirmed_usd']==0.25
    assert budget.summary()['unknown_billing_calls']==0


def test_redundant_pointer_regex_is_removed_only_when_enum_preserves_constraint():
    import jsonschema
    from validator.scientific_result_v1.source_tasks.proprietary_diagnostic import provider_schema
    def schema(values):
        return {'type':'object','properties':{'pointer':{'type':'string','pattern':'^/results/', 'enum':values}},
                'required':['pointer'],'additionalProperties':False}
    original=schema(['/results/1','/results/2'])
    wire=provider_schema(original)
    pointer=wire['properties']['result']['properties']['pointer']
    assert 'pattern' not in pointer and pointer['enum']==original['properties']['pointer']['enum']
    for value in ['/results/1','/results/2','/invented','']:
        assert jsonschema.Draft202012Validator(original).is_valid({'pointer':value})==jsonschema.Draft202012Validator(wire).is_valid({'result':{'pointer':value}})
    assert provider_schema(schema(['/results/1','bad']))['properties']['result']['properties']['pointer']['pattern']=='^/results/'
    # Deferring a large enum does not erase its remaining format restriction.
    large=schema(['/results/'+str(i)+'/long-scientific-field-name'*5 for i in range(795)])
    assert provider_schema(large)['properties']['result']['properties']['pointer']['pattern']=='^/results/'


def test_parallel_dspy_profiles_freeze_twenty_workers_and_isolate_scoped_usage(tmp_path):
    from validator.scientific_result_v1.source_tasks.configuration import freeze_settings
    from validator.scientific_result_v1.source_tasks.runner import make_operations,APPROVED_MODELS
    from validator.scientific_result_v1.source_tasks.store import Store
    from validator.scientific_result_v1.source_tasks.proprietary_diagnostic import execution_profiles
    from validator.scientific_result_v1.source_tasks.diagnostic_dispatch import worker_limit
    models={'SOURCE_READER':'openai/gpt-6-luna','PRIMARY':'openai/gpt-6-luna',
            'SECONDARY':'openai/gpt-5.4-mini','ARBITER':'openai/gpt-5.5'}
    profiles={r:policy(m,context_window_tokens=400000,budget_database=tmp_path/'budget.sqlite3',unlimited=True,
                      max_output_tokens=65536,max_in_flight=20) for r,m in models.items()}
    snapshots={m:{'id':m,'architecture':{'input_modalities':['text']},'endpoints':[
        {'provider_name':'automatic','context_length':400000,'max_completion_tokens':128000,
         'supported_parameters':['structured_outputs']}]} for m in set(models.values())}
    store=Store(tmp_path/'store')
    settings,_=freeze_settings(store,route_snapshots=snapshots,models=models,diagnostic_profiles=profiles,
        execution_policy={'mode':'local_proprietary_comparison_v1'})
    ops=make_operations(store=store,settings=settings,policy_sha256='b'*64,trace_dir=tmp_path/'traces',offline=True)
    child=ops.scoped()
    assert ops.parallel_diagnostic and worker_limit(ops)==worker_limit(child)==20
    assert worker_limit(ops,nested=True)==4
    assert child.gate is ops.gate
    for role in models:
        assert child.runtimes[role] is not ops.runtimes[role]
        assert child.runtimes[role].usage is not ops.runtimes[role].usage
    acquired=[ops.gate.acquire(blocking=False)for _ in range(21)]
    assert acquired==[True]*20+[False]
    for _ in range(20):ops.gate.release()
    assert not set(models.values())&APPROVED_MODELS
    broken={**profiles,'PRIMARY':{**profiles['PRIMARY'],'max_in_flight':4}}
    with pytest.raises(ValueError,match='one ledger and concurrency'):
        execution_profiles({'mode':'local_proprietary_comparison_v1','diagnostic_model_profiles':broken})


@pytest.mark.parametrize('limit',[0,21,True,1.5])
def test_parallel_diagnostic_limits_are_explicit_and_bounded(tmp_path,limit):
    with pytest.raises(ValueError,match='concurrency'):
        policy('openai/gpt-6-luna',context_window_tokens=1050000,budget_database=tmp_path/'budget.sqlite3',
               unlimited=True,max_in_flight=limit)


def test_schema_feedback_reports_actual_field_not_a_dump_of_the_response():
    from validator.scientific_result_v1.source_tasks.proprietary_diagnostic import compatible_adapter
    schema={'type':'object','properties':{'outcome':{'type':'string'},'pointer':{'type':'string','enum':['/shown/columns']},
            'explanation':{'type':'string'}},'required':['outcome','pointer','explanation'],'additionalProperties':False}
    answer={'result':{'outcome':'ASSESSED','pointer':'/shown/columns/99','explanation':'PRIVATE RESPONSE TEXT '*1000}}
    with pytest.raises(ValueError)as exc:compatible_adapter(schema).parse(None,json.dumps(answer))
    message=str(exc.value)
    assert '/pointer' in message and '/shown/columns/99' in message and 'allowed ancestor /shown/columns' in message
    assert 'PRIVATE RESPONSE TEXT' not in message and len(message)<1000


def test_schema_feedback_follows_authored_variant_without_unrelated_union_errors():
    import jsonschema
    from validator.scientific_result_v1.source_tasks.response_feedback import schema_feedback
    schema={'anyOf':[{'type':'object','properties':{'outcome':{'const':'ASSESSED'},'refs':{'type':'array','minItems':1,
        'items':{'oneOf':[{'type':'object','properties':{'kind':{'const':'json'},'pointer':{'enum':['/shown']}},'required':['kind','pointer']},
                         {'type':'object','properties':{'kind':{'const':'csv'},'filename':{'type':'string'}},'required':['kind','filename']}]}}}},
                    {'type':'object','properties':{'outcome':{'const':'NEED_EVIDENCE'},'refs':{'type':'array','maxItems':0}}}]}
    error=next(jsonschema.Draft202012Validator(schema).iter_errors({'outcome':'ASSESSED','refs':[{'kind':'json','pointer':'/invented'}]}))
    message=schema_feedback(error,schema)
    assert '/refs/0/pointer' in message and '/invented' in message
    assert 'filename' not in message and 'NEED_EVIDENCE' not in message


@pytest.mark.parametrize('review',[False,True])
def test_source_card_citations_are_limited_to_actual_supplied_anchor_ids(review):
    import jsonschema
    from validator.scientific_result_v1.source_tasks.contracts import SourceTaskBatch,SourceReviewResponse
    from validator.scientific_result_v1.source_tasks.response_schema import response_schema
    from validator.scientific_result_v1.source_tasks.proprietary_diagnostic import provider_schema
    allowed=['doc-text7-s0','doc-text7-s1']
    data={'authoritative_evidence':{'anchors':[{'anchor_id':v}for v in allowed]},
          'checked_anchor_ids':allowed,'checked_item_ids':['tile:one']}
    schema=response_schema(SourceReviewResponse if review else SourceTaskBatch,data)
    for name in ('SourceTaskDraft','SourceRequirement')+ (('SourceReviewIssue',)if review else ()):
        constraint=schema['$defs'][name]['properties']['evidence_anchor_ids']['items']
        for value in allowed:jsonschema.Draft202012Validator(constraint).validate(value)
        with pytest.raises(jsonschema.ValidationError):jsonschema.Draft202012Validator(constraint).validate('doc-text7')
    wire=provider_schema(schema)
    assert any(node.get('enum')==allowed for node in _schema_nodes(wire))


def _schema_nodes(value):
    if isinstance(value,dict):
        yield value
        for child in value.values():yield from _schema_nodes(child)
    elif isinstance(value,list):
        for child in value:yield from _schema_nodes(child)
