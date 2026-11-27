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
