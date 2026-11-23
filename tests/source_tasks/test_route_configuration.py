"""Frozen route settings reach the actual DSPy runtime and context accounting."""
import pytest
from validator.scientific_result_v1.source_tasks.configuration import MODELS, freeze_settings
from validator.scientific_result_v1.source_tasks.contracts import SourceTaskBatch
from validator.scientific_result_v1.source_tasks.operations import Hold
from validator.scientific_result_v1.source_tasks.runner import make_operations
from validator.scientific_result_v1.source_tasks.runtime import SourceTaskRuntime
from validator.scientific_result_v1.source_tasks.store import Store


def test_deepseek_secondary_and_larger_output_allowance_reach_runtime(tmp_path):
    store = Store(tmp_path / 'store')
    snapshots = {
        model: {'id': model, 'architecture': {'input_modalities': ['text']},
                'endpoints': [{'provider_name': 'test', 'context_length': 100000,
                               'supported_parameters': ['structured_outputs']}]}
        for model in set(MODELS.values())
    }
    settings, _ = freeze_settings(store, route_snapshots=snapshots, provider_allowlists={},
                                  execution_policy={'mode': 'test'})
    ops = make_operations(store=store, settings=settings, policy_sha256='1' * 64,
                          trace_dir=tmp_path / 'traces', offline=True)
    assert ops.routes['SECONDARY'].model == 'deepseek/deepseek-v4-flash-0731'
    assert all(route.max_output_tokens == 16384 for route in ops.routes.values())
    assert all(runtime.max_tokens == 16384 for runtime in ops.runtimes.values())
    assert all(runtime.reasoning_effort == 'none' for runtime in ops.runtimes.values())
    assert not ops.routes['SECONDARY'].supports_images
    assert settings.source_token_budgets == (128000, 128000, 128000)
    assert settings.structured_packet_tokens_per_shard == 64000
    # The full larger response allowance is reserved before any provider call.
    ops.tokenizers = {role: lambda _: 84000 for role in ops.routes}
    with pytest.raises(Hold, match='Route context window exceeded'):
        ops.request('PRIMARY', 'source_card', {}, SourceTaskBatch, source_tokens=0)
    with pytest.raises(ValueError, match='fixed provider parameters'):
        SourceTaskRuntime(model='test', api_key='', max_tokens=8192)


@pytest.mark.parametrize('source_tokens,packet_tokens,message', [
    (31000, 31000, 'uncached model operation'),
    (32000, 32000, 'uncached model operation'),
    (81000, 50000, 'uncached model operation'),
    (128000, 64000, 'uncached model operation'),
    (128001, 0, 'Source scope exceeds'),
    (0, 64001, 'Complete joint packet exceeds'),
])
def test_larger_review_guards_preserve_complete_bundles_and_reject_over_limit(
        tmp_path, source_tokens, packet_tokens, message):
    store = Store(tmp_path / 'store')
    snapshots = {
        model: {'id': model, 'architecture': {'input_modalities': ['text']},
                'endpoints': [{'provider_name': 'test', 'context_length': 200000,
                               'supported_parameters': ['structured_outputs']}]}
        for model in set(MODELS.values())
    }
    settings, _ = freeze_settings(store, route_snapshots=snapshots, provider_allowlists={}, execution_policy={})
    ops = make_operations(store=store, settings=settings, policy_sha256='1' * 64,
                          trace_dir=tmp_path / 'traces', offline=True)
    # Reaching the offline provider guard proves the real input-budget checks
    # accepted this request. No model call or permissive replacement is used.
    with pytest.raises(Hold, match=message):
        ops.request('PRIMARY', 'source_card', {}, SourceTaskBatch,
                    source_tokens=source_tokens, packet_tokens=packet_tokens)


def test_pdf_run_can_freeze_approved_image_reviewer_without_changing_default(tmp_path):
    store = Store(tmp_path / 'store')
    models = {**MODELS, 'SECONDARY': 'minimax/minimax-m3.1-flash'}
    snapshots = {model: {'id': model, 'architecture': {'input_modalities': ['text', 'image']},
                        'endpoints': [{'provider_name': 'test', 'context_length': 100000,
                                       'supported_parameters': ['structured_outputs']}]}
                 for model in set(models.values())}
    settings, _ = freeze_settings(store, route_snapshots=snapshots, provider_allowlists={},
                                  execution_policy={'mode': 'retrospective_pdf_check'}, models=models)
    ops = make_operations(store=store, settings=settings, policy_sha256='1' * 64,
                          trace_dir=tmp_path / 'traces', offline=True)
    assert ops.routes['SECONDARY'].supports_images
    assert ops.runtimes['SECONDARY'].reasoning_effort == 'minimal'
    assert MODELS['SECONDARY'] == 'deepseek/deepseek-v4-flash-0731'
    with pytest.raises(ValueError, match='Independent judge'):
        freeze_settings(store, route_snapshots=snapshots, provider_allowlists={}, execution_policy={},
                        models={**models, 'SECONDARY': models['PRIMARY']})


def test_default_routing_keeps_all_endpoints_and_does_not_pin_judge_providers(tmp_path):
    import json
    from validator.scientific_result_v1.source_tasks.contracts import FileRef
    store=Store(tmp_path/'store')
    snapshots={model:{'id':model,'architecture':{'input_modalities':['text']},
        'endpoints':[{'provider_name':'test','context_length':200000,'supported_parameters':['structured_outputs']}]}
        for model in set(MODELS.values())}
    deepseek=MODELS['SECONDARY']
    snapshots[deepseek]['endpoints']=[{'provider_name':name,'context_length':200000,
        'supported_parameters':['structured_outputs']} for name in ['Baidu','DeepInfra','Cohere']]
    settings,_=freeze_settings(store,route_snapshots=snapshots,execution_policy={})
    ops=make_operations(store=store,settings=settings,policy_sha256='1'*64,trace_dir=tmp_path/'traces',offline=True)
    for runtime in ops.runtimes.values():
        assert runtime.provider_preferences=={'require_parameters':True,'allow_fallbacks':True}
        lm=runtime._lm(stage='test',output_dir=tmp_path/'request',program_id='test')
        assert lm.kwargs['provider']=={'require_parameters':True,'allow_fallbacks':True}
    route=next(r for r in settings.model_routes if r.role=='SECONDARY')
    descriptor=json.loads(store.get_blob(route.route_sha256))
    original=json.loads(store.read(FileRef.model_validate(descriptor['original_snapshot'])))
    selected=json.loads(store.read(FileRef.model_validate(descriptor['snapshot'])))
    assert {e['provider_name'] for e in original['endpoints']}=={'Baidu','DeepInfra','Cohere'}
    assert {e['provider_name'] for e in selected['endpoints']}=={'Baidu','DeepInfra','Cohere'}
    assert len(snapshots[deepseek]['endpoints'])==3  # The original snapshot is not mutated.
    with pytest.raises(ValueError,match='provider allowlists are not supported'):
        freeze_settings(store,route_snapshots=snapshots,execution_policy={},provider_allowlists={deepseek:['missing']})


@pytest.mark.parametrize('preference',[{'only':['DeepInfra']},{'order':['Alibaba']},
    {'ignore':['Baidu']},{'allow_fallbacks':False},{'require_parameters':False}])
def test_runtime_rejects_provider_pins_and_preserves_strict_requirements(preference):
    with pytest.raises(ValueError):
        SourceTaskRuntime(model=MODELS['SECONDARY'],api_key='',provider_preferences=preference)


def test_historical_review_settings_are_readable_but_not_run_under_new_limits(tmp_path):
    from validator.scientific_result_v1.source_tasks.contracts import FrozenSettings
    store=Store(tmp_path/'store')
    snapshots={model:{'id':model,'architecture':{'input_modalities':['text']},
        'endpoints':[{'provider_name':'test','context_length':262144,'supported_parameters':['structured_outputs']}]}
        for model in set(MODELS.values())}
    settings,_=freeze_settings(store,route_snapshots=snapshots,execution_policy={})
    old=settings.model_dump(mode='json')
    old.update(source_token_budgets=[32000,32000,32000],structured_packet_tokens_per_shard=32000)
    import json
    historical=FrozenSettings.model_validate_json(json.dumps(old),strict=True)
    with pytest.raises(ValueError,match='Runtime review budgets differ'):
        make_operations(store=store,settings=historical,policy_sha256='1'*64,trace_dir=tmp_path/'traces',offline=True)
    old['structured_packet_tokens_per_shard']=64000
    with pytest.raises(ValueError,match='Unsupported combination'):
        FrozenSettings.model_validate_json(json.dumps(old),strict=True)
