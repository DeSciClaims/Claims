"""Reader parity and exact all-entrant unmeasured dispositions."""
import importlib.util
from pathlib import Path
import hashlib,json
import pytest

spec=importlib.util.spec_from_file_location('preparation_reader',Path(__file__).parents[1]/'scripts/scientific_preparation_exclusion_reader.py')
reader=importlib.util.module_from_spec(spec);spec.loader.exec_module(reader)

def test_same_common_pair_contract_as_backend():
    receipt={'kind':reader.KIND,'round_id':'r','stage':'final','paper_id':'p',
        'configuration_sha256':'c'*64,'source_sha256':'s'*64,'applies_to_all_entrants':True,
        'score_measured':False,'execution_attempted':False,'reason':'PREPARATION_DEADLINE_EXHAUSTED'}
    raw=json.dumps(receipt).encode();row={**receipt,'receipt_sha256':hashlib.sha256(raw).hexdigest()}
    assert reader._validate(row,raw,{'round_id':'r','configuration_sha256':'c'*64})==receipt
    entries=[{'hotkey':'b','package_sha256':'1'*64,'role':'reference'},
             {'hotkey':'a','package_sha256':'2'*64,'role':'challenger'}]
    pair={'stage':'final','paper_id':'p','configuration_sha256':'c'*64,'challenger_hotkey':'a',
        'challenger_package_sha256':'2'*64,'baseline_hotkey':'b','baseline_package_sha256':'1'*64,
        'challenger_score':0,'baseline_score':0,'infrastructure_fault':True,
        'details_public':{'protocol_id':'claims-source-task-v1','evaluation_status':'COMMON_EXCLUDED',
            'preparation_exclusion_sha256':row['receipt_sha256'],'scientific_score_measured':False}}
    reader._pairs(row,[pair],entries,'c'*64)
    with pytest.raises(ValueError):reader._pairs(row,[],entries,'c'*64)
    pair['details_public']['scientific_score_measured']=True
    with pytest.raises(ValueError):reader._pairs(row,[pair],entries,'c'*64)
    with pytest.raises(ValueError):reader._validate(row,raw,{'round_id':'r','configuration_sha256':'x'*64})


def test_nested_worker_audit_keeps_full_source_sample_for_exclusion_checks(monkeypatch):
    from types import SimpleNamespace
    from contextlib import contextmanager
    from competition import scientific_lineage,scientific_scoring,stage_controller,weekly_automation
    from competition import code_audit,worker_security_audit,pilot_controller_factory
    from validator.scientific_result_v1.source_tasks.store import canonical,sha
    rid='round';config='c'*64;source='a'*64
    record={'round_id':rid,'configuration_sha256':config,'rules_public':{'configuration':{'scientific_scoring':{
        'first_round_scoring':{'round_id':rid},'experimental_launch':{'round_id':rid}}}}}
    clock={'binding':sha(canonical([rid,'excluded',source,config])),'deadline':10}
    clock_raw=canonical(clock);clock_ref={'sha256':sha(clock_raw),'byte_length':len(clock_raw)}
    value={'kind':reader.KIND,'round_id':rid,'stage':'final','paper_id':'excluded',
        'configuration_sha256':config,'source_sha256':source,'applies_to_all_entrants':True,
        'score_measured':False,'execution_attempted':False,'reason':'PREPARATION_DEADLINE_EXHAUSTED',
        'preparation_clock':clock_ref,'recorded_at_epoch':11}
    raw=canonical(value);row={**value,'receipt_sha256':sha(raw)}
    store=SimpleNamespace(get_blob=lambda h:{sha(raw):raw,sha(clock_raw):clock_raw,'plan':canonical({'comparison_plan':{'sha256':'p'*64}})}[h],
        head=lambda k:'plan')
    @contextmanager
    def evidence(backend):yield store
    monkeypatch.setattr(scientific_lineage,'evidence_store',evidence)
    monkeypatch.setattr(scientific_lineage,'verify_current_grid',lambda rid,stage,**kw:[{'paper_id':'scored','verified':True}])
    monkeypatch.setattr(reader,'_get',lambda backend,rid:[row])
    def audit(rid,*,backend,**kw):
        assert [p['paper_id'] for p in backend.list_sample(rid)]==['scored']
        return scientific_lineage.verify_current_grid(rid,'final',backend=backend,
            pairs=[{'paper_id':'scored'}],round_record=record)
    monkeypatch.setattr(code_audit,'collect_final_code_evidence',audit)
    # Register every installed hook for restoration after this test.
    for module,name in [(stage_controller,'advance_ready_stage'),(weekly_automation,'advance_ready_stage'),
        (scientific_scoring,'scientific_copy_diagnostics'),(pilot_controller_factory,'collect_final_code_evidence'),
        (worker_security_audit,'audit_final_worker_security'),(weekly_automation,'audit_final_worker_security')]:
        monkeypatch.setattr(module,name,getattr(module,name))
    backend=SimpleNamespace(get_round=lambda rid:record,
        list_sample=lambda rid:[{'stage':'final','paper_id':'excluded','source_sha256':source},
            {'stage':'final','paper_id':'scored','source_sha256':'b'*64}],
        list_stages=lambda rid:[{'stage':'final','entries':[]}])
    reader.install(round_id=rid,configuration_sha256=config)
    assert code_audit.collect_final_code_evidence(rid,backend=backend)==[{'paper_id':'scored','verified':True}]
    backend.list_sample=lambda rid:[{'stage':'final','paper_id':'scored','source_sha256':'b'*64}]
    with pytest.raises(ValueError,match='differs from frozen source sample'):
        code_audit.collect_final_code_evidence(rid,backend=backend)
