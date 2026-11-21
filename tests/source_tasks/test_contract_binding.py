"""The amended guide, installed extraction schema and executable wire contracts agree."""
from pathlib import Path
import ast
import hashlib
import json
import re
import pytest
from validator.scientific_result_v1.source_tasks import contracts
from validator.scientific_result_v1.source_tasks.packages import SCHEMA_HASH,SCHEMA_PATH
from validator.scientific_result_v1.source_tasks.runtime import StrictAdapter


def test_extended_schema_and_all_reference_contracts_match_amended_guide():
    root=Path(__file__).parents[2]
    guide=(root/'schemas/scientific-result-extraction/v1.1/VALIDATION_GUIDE.md').read_text()
    raw=(root/'schemas/scientific-result-extraction/v1.1/schema.json').read_bytes()
    assert hashlib.sha256(raw).hexdigest()==SCHEMA_HASH
    assert SCHEMA_PATH.read_bytes()==raw
    assert SCHEMA_HASH in guide and '3d2bcec0' not in guide
    block=re.search(r'```python\n(from __future__ import annotations[\s\S]+?)\n```',guide).group(1)
    assert ast.dump(ast.parse(block))==ast.dump(ast.parse(Path(contracts.__file__).read_text()))
    embedded=json.loads(re.findall(r'```json\n([\s\S]+?)\n```',guide)[-1])
    assert embedded==json.loads(raw)
    assert 'unmeasured_without_truth_corpus' in guide


def test_strict_judge_adapter_does_not_strip_fences_or_allow_duplicate_keys():
    adapter=StrictAdapter({'type':'object'})
    assert adapter.parse(None,'{"ok":true}')=={'ok':True}
    for bad in ['```json\n{"ok":true}\n```','{"ok":true,"ok":false}','{} trailing']:
        with pytest.raises(ValueError):adapter.parse(None,bad)


def test_provider_schema_enforces_existing_status_and_counterexample_rules():
    import copy
    import jsonschema
    from validator.scientific_result_v1.source_tasks.contracts import DimensionCheck
    schema=DimensionCheck.model_json_schema()
    counter=dict(code='VALUE',rule_ids=['V09'],description='Value differs',evidence_anchor_ids=['source'],
        output_refs=[dict(kind='json',output_alias='a',pointer='/results/0/fields/0/value')],missing_requirement_id=None)
    check=dict(dimension='NUMERIC_FIDELITY',status='PASS',evidence_anchor_ids=['source'],counterexamples=[])
    jsonschema.validate(check,schema)
    invalid=copy.deepcopy(check);invalid['counterexamples']=[counter]
    with pytest.raises(jsonschema.ValidationError):jsonschema.validate(invalid,schema)
    failure={**invalid,'status':'FAIL'};jsonschema.validate(failure,schema)
    with pytest.raises(jsonschema.ValidationError):jsonschema.validate({**check,'status':'FAIL'},schema)
    with pytest.raises(jsonschema.ValidationError):jsonschema.validate({**check,'evidence_anchor_ids':[]},schema)
    jsonschema.validate({**check,'status':'NOT_APPLICABLE','evidence_anchor_ids':[]},schema)
