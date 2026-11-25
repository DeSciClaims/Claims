from types import SimpleNamespace
from dataclasses import replace
from threading import Barrier
import pytest
from competition import scientific_factory as factory
from competition.code_audit import SOURCE_AUDIT_POLICY
from competition.scientific_first_round import first_round_policy
from tests.test_competition_scientific_launch import fixture as launch_fixture
from validator.scientific_result_v1.source_tasks.configuration import freeze_settings,MODELS
from validator.scientific_result_v1.source_tasks.budgeted_validation import PROFILE
from validator.scientific_result_v1.source_tasks.store import Store,canonical,sha
from validator.scientific_result_v1.source_tasks.packages import SCHEMA_HASH


def activated(tmp_path,monkeypatch):
    store=Store(tmp_path/'store');launch,basis=launch_fixture()
    launch['basis_assessment']=store.put(basis).model_dump(mode='json')
    snapshots={m:{'id':m,'architecture':{'input_modalities':['text']},'endpoints':[dict(provider_name='fixture',supported_parameters=['structured_outputs'],
        context_length=262144,max_completion_tokens=32768)]} for m in set(MODELS.values())}
    settings,ref=freeze_settings(store,route_snapshots=snapshots,execution_policy={
        'mode':'experimental_mainnet_launch','budgeted_validation':dict(PROFILE)},sealed_evaluation_timeout_seconds=7200)
    spec=dict(protocol_id='claims-source-task-v1',schema_sha256=SCHEMA_HASH,source_scope_policy='supplied_sources_only',
        organization_score=False,canonical_organization=False,winner_selection='silver_tiebreak',mode='production',
        settings=ref.model_dump(mode='json'),settings_sha256=sha(canonical(settings)),
        scientific_accuracy_policy='unmeasured_without_truth_corpus',first_round_scoring=first_round_policy('round_mainnet'),
        experimental_launch=launch,optimized_validation=dict(PROFILE),runtime_controls=dict(
        kind='claims_local_diagnostic_runtime_controls_v1',shared_max_llm_requests=500,max_provider_attempts=1,max_submissions_per_paper=4))
    config={'scientific_scoring':spec,'worker_policy':{'output_schema':'scientific_result_v1.1'}}
    row=dict(round_id='round_mainnet',network='mainnet',incumbent_hotkey=None,configuration_sha256=sha(canonical(config)),
        rules_public={'policy':{'validation_protocol':'claims-source-task-v1','winner_selection':'silver_tiebreak','code_audit':SOURCE_AUDIT_POLICY}})
    monkeypatch.setattr(factory,'RemoteStore',lambda *a:store)
    monkeypatch.setattr(factory,'_root',lambda:tmp_path)
    monkeypatch.setenv('OPENROUTER_API_KEY','test-only')
    return row,config


def test_mainnet_optimized_mode_has_real_shared_limit_and_no_decision_api(tmp_path,monkeypatch):
    row,config=activated(tmp_path,monkeypatch);policy=factory.create_scientific_policy(row,object(),config)
    ops=policy.evaluate_pool.operations_for_paper('round_mainnet','prescreen','paper',row['configuration_sha256'])
    assert ops.budgeted_profile==PROFILE and ops.gate.limit==500 and ops.max_workers==20
    assert ops.diagnostic_runtime_controls['max_submissions_per_paper']==4
    class ReachedScientificPool(Exception):pass
    def pool(**kwargs):
        assert kwargs['stage'] in {'prescreen','screen','final'}
        raise ReachedScientificPool
    replacement=replace(policy,evaluate_pool=pool)
    for stage in ['prescreen','screen','final']:
        with pytest.raises(ReachedScientificPool):replacement.evaluate_for_stage(stage)(stage=stage,paper_id='paper')
    assert 'decision_routing' not in config


def test_optimized_profile_must_be_publicly_advertised(tmp_path,monkeypatch):
    row,config=activated(tmp_path,monkeypatch);config['scientific_scoring'].pop('optimized_validation')
    row['configuration_sha256']=sha(canonical(config))
    with pytest.raises(ValueError,match='differs from frozen'):factory.create_scientific_policy(row,object(),config)


def test_source_preparation_honors_workers_and_isolates_duplicate_pdf_paths(tmp_path,monkeypatch):
    store=Store(tmp_path/'source-store');barrier=Barrier(2);paths=[]
    wrapper=SimpleNamespace(head=store.head,generation=store.generation,
        put_existing_source=lambda r,p,raw:store.put_named(p+'.pdf',raw,'application/pdf'))
    policy=SimpleNamespace(evaluate_pool=SimpleNamespace(store=wrapper,
        operations_for_paper=lambda *a:SimpleNamespace(deadline=0)))
    monkeypatch.setattr(factory,'create_scientific_policy',lambda *a:policy)
    monkeypatch.setattr(factory,'_root',lambda:tmp_path)
    def prepare(**kwargs):
        paths.append(next(iter(kwargs['parsed_directories'].values())))
        barrier.wait(timeout=3)
    monkeypatch.setattr('validator.scientific_result_v1.source_tasks.preparation.prepare_paper',prepare)
    backend=SimpleNamespace(list_sample=lambda _: [{'paper_id':p,'stage':'prescreen'} for p in ['a','b']],
        get_scientific_source=lambda *a:{'source_url':'https://example.test/paper','source_sha256':sha(b'pdf')})
    row={'round_id':'round','configuration_sha256':'1'*64,'rules_public':{'policy':{'execution':{
        'schema_version':'claims_competition_execution_v1','bronze_max_workers':2,'scoring_max_workers':10,'decision_max_workers':20}}}}
    preparer=factory.create_source_preparer(row,backend,{'scientific_scoring':{}},SimpleNamespace(fetch=lambda *a:b'pdf'))
    assert preparer('round')['prepared_papers']==['a','b'] and len(set(paths))==2
