"""Distinct-source identity exclusions cannot hide errors in the same scientific owner."""
import json
from pathlib import Path
import pytest
from validator.scientific_result_v1.source_tasks.contracts import (
    IdentityDecision,IdentityReviewResponse,RequirementMatch,
)
from validator.scientific_result_v1.source_tasks.match_identity import (
    owner_groups,group_requirements,apply_reviews,validate_review,scientific_owner,
)
from validator.scientific_result_v1.source_tasks.response_schema import response_schema
from validator.scientific_result_v1.source_tasks.store import Store,canonical
from validator.scientific_result_v1.source_tasks.packets import Index
from validator.scientific_result_v1.source_tasks.packages import Package
from tests.source_tasks.test_source_scoring import generated,packet_case


def setup_case(generated):
    path,doc,_=generated
    # A wrong number for the SAME scientific owner must survive identity filtering.
    doc['results'][0]['fields'][0]['value']=999
    (path/'output.json').write_text(json.dumps(doc))
    index=Index(Package(path,'a'));store=Store(path.parent/'identity-unit')
    results=[o for o in index.objects if o.kind=='RESULT'];refs=[o.original_ref for o in results]
    for obj in index.objects:
        if obj.kind=='FIELD' and obj.original_ref.pointer.startswith('/results/1/'):refs.append(obj.original_ref)
    task,packet=packet_case(index,store,refs=refs)
    match=packet.match.model_copy(update={'matches':[RequirementMatch(requirement_id=task.requirements[0].requirement_id,output_refs=refs)]})
    groups=owner_groups(index,match);required=group_requirements(match,groups)
    return index,task,match,groups,required


def review(task,groups,required,second='KEEP'):
    return IdentityReviewResponse(task_id=task.task_id,output_alias='a',inspected_owner_ids=list(groups),
        decisions={key:{root:IdentityDecision(disposition=second if root=='/results/1' else 'KEEP',
            distinction_dimension='OUTCOME' if root=='/results/1' and second=='DISTINCT_SOURCE_RESULT' else None,
            separate_source_identity='Another source-defined outcome' if root=='/results/1' and second!='KEEP' else None,
            evidence_anchor_ids=['source'] if root=='/results/1' and second!='KEEP' else [],
            reason='Same referent, including wrong numbers' if root!='/results/1' or second=='KEEP' else 'Source establishes a distinct outcome')
            for root in roots} for key,roots in required.items()})


@pytest.mark.parametrize('other_role',['KEEP','DISTINCT_SOURCE_RESULT'])
def test_two_positive_reviews_are_required_and_whole_owner_errors_survive(generated,other_role):
    index,task,match,groups,required=setup_case(generated)
    a=review(task,groups,required,'DISTINCT_SOURCE_RESULT');b=review(task,groups,required,other_role)
    for response in [a,b]:validate_review(response,task,'a',groups,required,{'source'},{r for r in groups if scientific_owner(index,r)})
    result=apply_reviews(match,groups,[a,b])
    expected=groups['/results/0'] if other_role=='DISTINCT_SOURCE_RESULT' else match.candidate_refs
    assert {canonical(r) for r in result.candidate_refs}=={canonical(r) for r in expected}
    packet=index.packet(task,task.binding,result,'5'*64,[])
    assert any(p.name=='value' and p.value_json=='999' for o in packet.objects for p in o.properties)
    assert result.coverage==match.coverage  # Original exhaustive search is unchanged.


def test_scientific_owner_cannot_be_excluded_for_representation_format(generated):
    index,task,match,groups,required=setup_case(generated)
    response=review(task,groups,required,'CONTEXT_ONLY')
    with pytest.raises(ValueError,match='cannot be excluded as mere context'):
        validate_review(response,task,'a',groups,required,{'source'},set(groups))


def test_exclusion_needs_positive_source_witness_and_explicit_distinction():
    with pytest.raises(ValueError,match='positive source identity'):
        IdentityDecision(disposition='DISTINCT_SOURCE_RESULT',distinction_dimension='ANALYSIS',
            separate_source_identity='Other analysis',evidence_anchor_ids=[],reason='Different')
    with pytest.raises(ValueError,match='distinction dimension'):
        IdentityDecision(disposition='DISTINCT_SOURCE_RESULT',distinction_dimension=None,
            separate_source_identity='Other analysis',evidence_anchor_ids=['source'],reason='Different')


def test_bound_identity_schema_requires_every_owner_and_source_proof(generated):
    import copy,jsonschema
    index,task,match,groups,required=setup_case(generated)
    data={'task':task.model_dump(mode='json'),'output_alias':'a','inspection_owner_ids':list(groups),
        'scientific_owner_ids':list(groups),'required_owner_ids_by_requirement':required,
        'authoritative_evidence':{'anchors':[{'anchor_id':'source'}]}}
    schema=response_schema(IdentityReviewResponse,data)
    answer=review(task,groups,required,'DISTINCT_SOURCE_RESULT').model_dump(mode='json')
    jsonschema.validate(answer,schema)
    missing=copy.deepcopy(answer);del missing['decisions'][task.requirements[0].requirement_id]['/results/1']
    with pytest.raises(jsonschema.ValidationError):jsonschema.validate(missing,schema)
    wrong=copy.deepcopy(answer);decision=wrong['decisions'][task.requirements[0].requirement_id]['/results/1'];decision.update(disposition='CONTEXT_ONLY',distinction_dimension=None)
    with pytest.raises(jsonschema.ValidationError):jsonschema.validate(wrong,schema)
    wrong=copy.deepcopy(answer);wrong['decisions'][task.requirements[0].requirement_id]['/results/1']['evidence_anchor_ids']=['invented']
    with pytest.raises(jsonschema.ValidationError):jsonschema.validate(wrong,schema)


def test_owner_batches_require_complete_disjoint_coverage_for_each_role(generated):
    from validator.scientific_result_v1.source_tasks.match_identity import merge_reviews
    index,task,match,groups,required=setup_case(generated)
    roots=list(groups);responses=[]
    for root in roots:
        subset={root:groups[root]};requirements={key:[r for r in owners if r==root] for key,owners in required.items()}
        responses.append(review(task,subset,requirements,'DISTINCT_SOURCE_RESULT'))
    merged=merge_reviews(task,'a',groups,required,responses)
    assert merged.inspected_owner_ids==list(groups)
    assert set(merged.decisions[task.requirements[0].requirement_id])==set(groups)
    with pytest.raises(ValueError,match='unresolved'):merge_reviews(task,'a',groups,required,responses[:1])
    with pytest.raises(ValueError,match='twice'):merge_reviews(task,'a',groups,required,[responses[0],responses[0]])
