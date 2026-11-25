import json

from validator.scientific_result_v1.source_tasks.file_judge import materialize,WorkspaceTools
from validator.scientific_result_v1.source_tasks.file_judge_audit import access_audit
from validator.scientific_result_v1.source_tasks.response_schema import response_schema
from validator.scientific_result_v1.source_tasks.contracts import SourceTaskBatch
from tests.source_tasks.test_file_judge import request
from tests.source_tasks.test_source_context import inputs,ready


def test_type_feedback_explains_expected_array_before_any_large_invalid_value():
    import jsonschema
    from validator.scientific_result_v1.source_tasks.file_judge import validation_feedback
    schema={'type':'object','properties':{'requirements':{'type':'array'}}}
    wrong={'requirements':{'item':{'statement':'Long scientific context '*2000}}}
    error=next(jsonschema.Draft202012Validator(schema).iter_errors(wrong))
    feedback=validation_feedback(error)
    assert '/requirements (type)' in feedback and "Expected JSON type 'array'; got object" in feedback
    assert len(feedback)<300 and 'Long scientific context' not in feedback


def test_union_feedback_selects_matching_outcome_and_explains_wrong_identity():
    import jsonschema
    from validator.scientific_result_v1.source_tasks.file_judge import validation_feedback
    schema={'anyOf':[
        {'type':'object','properties':{'outcome':{'const':'ACCEPT'},'checked_ids':{'const':['original:card','proposed:card']},'corrected':{'type':'null'}}},
        {'type':'object','properties':{'outcome':{'const':'REVISE'},'corrected':{'type':'object'}}},
    ]}
    wrong={'outcome':'ACCEPT','checked_ids':['invented-requirement'],'corrected':None}
    error=next(jsonschema.Draft202012Validator(schema).iter_errors(wrong))
    feedback=validation_feedback(error)
    assert '/checked_ids (const)' in feedback and 'original:card' in feedback and 'invented-requirement' in feedback
    assert 'corrected' not in feedback


def test_long_constant_error_identifies_field_and_tail_difference_without_repair(tmp_path):
    req=request();ids=['component-'+str(i).zfill(5) for i in range(1000)]
    req['stage_schema']={'type':'object','properties':{'inspected_component_ids':{'const':ids}},
                         'required':['inspected_component_ids'],'additionalProperties':False}
    materialize(tmp_path/'work',req);tools=WorkspaceTools(tmp_path/'work')
    result=tools.submit({'inspected_component_ids':ids[:-1]+['invented']})
    assert not result['accepted'] and not (tmp_path/'work'/'output.json').exists()
    error=result['error']
    assert '/inspected_component_ids (const)' in error
    assert 'component-00999' in error and 'invented' in error
    assert len(error)<1000


def test_combined_component_view_preserves_values_and_proves_only_read_spans(tmp_path):
    req=request();req['operation']='source_match'
    components={'/results/0':{'value':'3.500 ± 0.070'},'/results/1':{'value':'0.00042'}}
    req['payload']['components']=components
    root=tmp_path/'work';materialize(root,req);tools=WorkspaceTools(root)
    assert json.loads((root/'candidate/components.json').read_text())==components
    tools.read('candidate/components.json',0,5)
    assert access_audit(root)['fully_read_matching_components']==0
    tools.read('candidate/components.json',5)
    assert access_audit(root)['fully_read_matching_components']==2


def card_request(operation):
    tile,_,evidence=inputs();evidence['anchors'].append({'anchor_id':'context','exact_text':'Neighboring finding'})
    data={'tile':tile.model_dump(mode='json'),'tile_id':tile.tile_id,'authoritative_evidence':evidence}
    return {'operation':operation,'instructions':'Preserve ownership.','payload':data,
            'stage_schema':response_schema(SourceTaskBatch,data)}


def test_source_card_gets_named_ownership_feedback_inside_same_agent(tmp_path):
    req=card_request('source_card');root=tmp_path/'work';materialize(root,req);tools=WorkspaceTools(root)
    result=tools.submit(SourceTaskBatch.model_validate(ready('context')).model_dump(mode='json'))
    assert not result['accepted'] and 'finding' in result['error'] and 'owned tile' in result['error']
    assert tools.submit(SourceTaskBatch.model_validate(ready()).model_dump(mode='json'))['accepted']


def test_consolidation_cannot_drop_original_witnesses_inside_agent(tmp_path):
    req=card_request('source_card_consolidate')
    req['payload']['required_preserved_evidence_anchor_ids']=['context']
    root=tmp_path/'work';materialize(root,req);tools=WorkspaceTools(root)
    result=tools.submit(SourceTaskBatch.model_validate(ready()).model_dump(mode='json'))
    assert not result['accepted'] and 'dropped original evidence' in result['error']
    corrected=SourceTaskBatch.model_validate(ready()).model_dump(mode='json');corrected['cards'][0]['evidence_anchor_ids'].append('context')
    assert tools.submit(corrected)['accepted']
