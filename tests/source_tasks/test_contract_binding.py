"""The amended guide, installed extraction schema and executable wire contracts agree."""
# ruff: noqa: F811
import ast
import hashlib
import json
import re
from pathlib import Path

import pytest

from tests.source_tasks.test_source_scoring import generated  # noqa: F401
from validator.scientific_result_v1.source_tasks import contracts
from validator.scientific_result_v1.source_tasks.packages import (
    SCHEMA_HASH,
    SCHEMA_PATH,
)
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


def test_family_failure_schema_requires_two_rendered_results(generated,tmp_path):
    import copy

    import jsonschema

    from tests.source_tasks.test_source_scoring import packet_case
    from validator.scientific_result_v1.source_tasks.contracts import JudgeResponse
    from validator.scientific_result_v1.source_tasks.packages import Package
    from validator.scientific_result_v1.source_tasks.packets import Index
    from validator.scientific_result_v1.source_tasks.response_schema import (
        response_schema,
    )
    from validator.scientific_result_v1.source_tasks.store import Store
    from validator.scientific_result_v1.source_tasks.transport import judge_packet
    path,_,_=generated;index=Index(Package(path,'a'));store=Store(tmp_path/'case')
    roots=[o.original_ref for o in index.objects if o.kind=='RESULT']
    task,packet=packet_case(index,store,refs=roots)
    wire={'task':task.model_dump(mode='json'),'packet':judge_packet(packet),'required_dimensions':['GROUPING_CONFORMANCE'],
          'authoritative_evidence':{'anchors':[{'anchor_id':'source'}]}}
    schema=response_schema(JudgeResponse,wire)
    counter=dict(code='SOURCE_SEPARATE',rule_ids=['V10'],description='Two concrete source-supported investigations were merged',evidence_anchor_ids=['source'],output_refs=[r.model_dump(mode='json') for r in roots[:2]],missing_requirement_id=None)
    scoped={'$defs':schema['$defs'],'$ref':'#/$defs/Counterexample'}
    jsonschema.validate(counter,scoped)
    one=copy.deepcopy(counter);one['output_refs'].pop()
    with pytest.raises(jsonschema.ValidationError):jsonschema.validate(one,scoped)
    no_rule=copy.deepcopy(counter);no_rule['rule_ids']=['V09']
    with pytest.raises(jsonschema.ValidationError):jsonschema.validate(no_rule,scoped)
    # Actual retained failure had no result records: a field/context packet must
    # not accept an invented family counterexample.
    empty=packet.model_copy(update={'objects':[],'context_headers':[]})
    constrained=response_schema(JudgeResponse,{**wire,'packet':judge_packet(empty)})
    with pytest.raises(jsonschema.ValidationError):jsonschema.validate(counter,{'$defs':constrained['$defs'],'$ref':'#/$defs/Counterexample'})
