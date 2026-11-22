from typing import Any

import dspy
import litellm

from miner.scientific_result_v1.programs import (
    InventoryProgram, ResultBatchProgram, SourceTableAuditProgram,
    FamilyGroupingProgram, RepairProgram, stage_program,
)
from miner.scientific_result_v1.runtime import DspyRuntime


def test_stage_registry_exposes_distinct_dspy_program_types():
    schema = {'type': 'object', 'properties': {'payload': {'type': 'object'}}, 'required': ['payload']}
    for stage, expected in [('inventory-1', InventoryProgram), ('results-001-1', ResultBatchProgram),
                            ('source-table-audit-1', SourceTableAuditProgram), ('families-1', FamilyGroupingProgram),
                            ('repair-1-1', RepairProgram)]:
        program = stage_program(stage, schema=schema, instructions='Test')
        assert isinstance(program, expected)
        assert isinstance(program.predict, dspy.Predict)
        assert program.predict.signature.output_fields['payload'].annotation == dict[str, Any]


def test_optional_and_nested_nulls_are_preserved(monkeypatch, tmp_path):
    def completion(**kwargs):
        return litellm.ModelResponse(model='test', choices=[{'index': 0, 'finish_reason': 'stop',
            'message': {'role': 'assistant', 'content': '{"payload": {"missing_value": null}, "optional_section": null}'}}],
            usage={'prompt_tokens': 1, 'completion_tokens': 1, 'total_tokens': 2})
    monkeypatch.setattr(litellm, 'completion', completion)
    schema = {'type': 'object', 'properties': {'payload': {'type': 'object'}, 'optional_section': {'type': 'object'}}, 'required': ['payload']}
    runtime = DspyRuntime(model='deepseek/deepseek-v4-flash-0731', api_key='test')
    with dspy.context(track_usage=True):
        result = runtime.generate(instructions='Test', payload={'stage_schema': schema}, output_dir=tmp_path/'stages', stage='inventory-1')
    assert result == {'payload': {'missing_value': None}, 'optional_section': None}


def test_no_table_inventory_supports_boolean_json_schema_items():
    schema = {'type': 'object', 'properties': {
        'excluded_candidates': {'type': 'array', 'items': False},
        'result_plan': {'type': 'array', 'items': {'type': 'object'}},
    }, 'required': ['excluded_candidates', 'result_plan']}
    program = stage_program('inventory-1', schema=schema, instructions='No table candidates')
    assert program.predict.signature.output_fields['excluded_candidates'].annotation == list[Any]


def test_adapter_preserves_raw_prompt_and_multimodal_order(tmp_path):
    import base64
    import json
    from miner.scientific_result_v1.programs import JSONStageAdapter

    image_path = tmp_path / 'page.png'
    image_path.write_bytes(b'source-page-bytes')
    instructions = 'Extract all results.\nKeep source wording.\n'
    payload = {'source': 'μ ± α', 'stage_schema': {'type': 'object'},
               'source_images': [{'source_id': 'S001', 'page': 2, 'path': str(image_path)}]}
    program = stage_program('inventory', schema={'properties': {}, 'required': []}, instructions=instructions)
    messages = JSONStageAdapter().format(program.predict.signature, [], {'stage_request': payload})
    assert messages == [
        {'role': 'system', 'content': instructions},
        {'role': 'user', 'content': [
            {'type': 'text', 'text': json.dumps({'source': 'μ ± α', 'stage_schema': {'type': 'object'}}, ensure_ascii=False)},
            {'type': 'text', 'text': 'Source S001, PDF page 2'},
            {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,' + base64.b64encode(b'source-page-bytes').decode('ascii')}},
        ]},
    ]
