"""Source-card context requests retrieve retained source, without moving ownership."""
import pytest

from validator.scientific_result_v1.source_tasks.contracts import (
    EvaluationBinding, SourceTaskBatch, SourceReviewResponse, SourceTile,
)
from validator.scientific_result_v1.source_tasks.operations import Hold
from validator.scientific_result_v1.source_tasks.source import selected_cards


def inputs():
    tile = SourceTile(tile_id='tile', document_uid='d1', stratum='PROSE_OR_EQUATION',
        eligible_content=True, owned_anchor_ids=['owned'], context_anchor_ids=[],
        table_uid=None, first_data_row=None, stop_data_row=None)
    binding = EvaluationBinding(protocol_id='claims-source-task-v1', paper_id='P005',
        round_id='r', stage='final', source_packet_sha256='1'*64,
        source_frame_sha256='2'*64, input_pool_sha256='3'*64,
        policy_sha256='4'*64, revision=0)
    return tile, binding, {'anchors': [{'anchor_id': 'owned', 'exact_text': 'Estimate 3.50.'}]}


def need():
    return dict(request_id='context', kind='ADJACENT_TEXT', anchor_ids=['owned'],
        document_uid='d1', query='Retrieve the associated definition and headers')


def ready(anchor='owned'):
    return dict(tile_id='tile', outcome='READY', cards=[dict(local_key='finding',
        task_kind='CONTENT', scope_kind='SCIENTIFIC_RESULT', requirements=[dict(requirement_id='estimate',
            statement='Represent the estimate with its definition', qualifications=[],
            evidence_anchor_ids=[anchor], encoding_constraints=[])],
        evidence_anchor_ids=[anchor], source_scope_note='Original owned finding')],
        evidence_requests=[], unresolved_reason=None)


class Script:
    def __init__(self, *, reviewer_requests=False, always_requests=False, wrong_owner=False):
        self.calls = []
        self.reviewer_requests = reviewer_requests
        self.always_requests = always_requests
        self.wrong_owner = wrong_owner

    def request(self, role, operation, data, response_type, *, validate, source_limit=128000):
        self.calls.append((role, operation, source_limit))
        assert data["controller_requirement_policy"]
        assert "Preserve each coherent parent card" in data["controller_requirement_policy"]
        assert "candidate" not in data and "packet" not in data
        if response_type is SourceTaskBatch:
            if operation=='source_card_consolidate':
                assert data['required_preserved_evidence_anchor_ids']==['owned']
                response=response_type.model_validate(data['original_source_draft'])
                validate(response)
                return response
            requested = self.always_requests or (len(self.calls) == 1 and not self.reviewer_requests)
            value = dict(tile_id='tile', outcome='NEED_EVIDENCE', cards=[],
                evidence_requests=[need()], unresolved_reason=None) if requested else ready(
                    'context' if self.wrong_owner else 'owned')
        else:
            assert response_type is SourceReviewResponse
            assert data['original_source_draft']
            assert data['checked_item_ids']==['original:finding','proposed:finding']
            requested = self.reviewer_requests and sum(op=='source_card_review' for _,op,_ in self.calls)==1
            value = dict(request_id=data['request_id'], proposal_kind=data['proposal_kind'],
                proposal_hash=data['proposal_hash'], outcome='NEED_EVIDENCE' if requested else 'ACCEPT',
                checked_anchor_ids=[] if requested else data['checked_anchor_ids'],
                checked_item_ids=[] if requested else data['checked_item_ids'], issues=[],
                corrected_proposal=None, evidence_requests=[need()] if requested else [],
                unresolved_reason=None)
        response = response_type.model_validate(value)
        validate(response)
        return response


@pytest.mark.parametrize('reviewer_requests', [False, True])
def test_source_reader_or_independent_review_can_request_retained_context(reviewer_requests):
    tile, binding, evidence = inputs()
    ops = Script(reviewer_requests=reviewer_requests)
    expanded = []
    def expand(current, requests, limit):
        expanded.append((requests, limit))
        return {'anchors': current['anchors'] + [{'anchor_id': 'context', 'exact_text': 'Units mg.'}]}
    cards = selected_cards(ops, tile, binding, evidence, expand=expand)
    assert len(cards) == 1 and cards[0].tile_ids == ['tile']
    assert cards[0].evidence_anchor_ids == ['owned']
    assert len(expanded) == 1 and expanded[0][1] == 128000
    assert ops.calls[-1] == ('SECONDARY', 'source_card_review', 128000)


def test_source_card_context_expansion_is_bounded():
    tile, binding, evidence = inputs()
    ops = Script(always_requests=True)
    limits = []
    def expand(current, requests, limit):
        limits.append(limit)
        return current
    with pytest.raises(Hold, match='context remains unresolved'):
        selected_cards(ops, tile, binding, evidence, expand=expand)
    assert limits == [128000, 128000]
    assert [call[2] for call in ops.calls] == [128000, 128000, 128000]


def test_expanded_context_cannot_become_an_unrelated_owned_target():
    tile, binding, evidence = inputs()
    with pytest.raises(ValueError, match='original owned tile'):
        selected_cards(Script(wrong_owner=True), tile, binding, evidence,
            expand=lambda current, requests, limit: {'anchors': current['anchors'] + [
                {'anchor_id': 'context', 'exact_text': 'Unrelated finding.'}]})


def test_source_scope_identity_is_explicit_in_provider_contract():
    import copy,jsonschema
    from validator.scientific_result_v1.source_tasks.response_schema import response_schema
    tile,binding,evidence=inputs();batch=SourceTaskBatch.model_validate(ready())
    schema=response_schema(SourceTaskBatch,{'tile_id':tile.tile_id})
    answer=batch.model_dump(mode='json');jsonschema.validate(answer,schema)
    missing=copy.deepcopy(answer);del missing['cards'][0]['requirements'][0]['adjustment_scope']
    with pytest.raises(jsonschema.ValidationError):jsonschema.validate(missing,schema)
    assert answer['cards'][0]['requirements'][0]['analysis_scope']=='SOURCE_UNSPECIFIED'


def test_scientific_shard_cannot_change_analysis_identity():
    from validator.scientific_result_v1.source_tasks.contracts import TaskCard
    from validator.scientific_result_v1.source_tasks.sharding import validate_child
    tile,binding,evidence=inputs();draft=SourceTaskBatch.model_validate(ready()).cards[0]
    parent=TaskCard(task_id='parent',task_kind='CONTENT',binding=binding,tile_ids=[tile.tile_id],
        output_alias=None,source_bucket_id=None,requirements=draft.requirements,
        evidence_anchor_ids=['owned'],source_scope_note=draft.source_scope_note)
    changed=parent.requirements[0].model_copy(update={'analysis_scope':'A different source analysis'})
    child=parent.model_copy(update={'task_id':'parent::shard:test','requirements':[changed]})
    with pytest.raises(ValueError,match='rewrote a scientific requirement'):validate_child(child,parent)


def test_consolidation_cannot_drop_original_source_witnesses():
    class DropsWitness(Script):
        def request(self, role, operation, data, response_type, *, validate, source_limit=128000):
            if operation in {'source_card','source_card_consolidate'}:
                value=ready()
                if operation=='source_card':value['cards'][0]['evidence_anchor_ids'].append('context')
                response=response_type.model_validate(value)
                validate(response)
                return response
            return super().request(role,operation,data,response_type,validate=validate,source_limit=source_limit)
    tile,binding,evidence=inputs()
    evidence['anchors'].append({'anchor_id':'context','exact_text':'A necessary definition.'})
    with pytest.raises(ValueError,match='dropped original evidence'):
        selected_cards(DropsWitness(),tile,binding,evidence)


def test_independent_source_correction_can_remove_an_unsupported_draft_claim():
    import copy
    tile,binding,evidence=inputs()
    tile=tile.model_copy(update={'owned_anchor_ids':['owned','garbled']})
    evidence['anchors'].append({'anchor_id':'garbled','exact_text':'Illegible OCR fragment'})
    original=ready();bad=copy.deepcopy(original['cards'][0]);bad['local_key']='unsupported'
    bad['evidence_anchor_ids']=['garbled'];bad['requirements'][0]['evidence_anchor_ids']=['garbled']
    original['cards'].append(bad)
    class Correct:
        def __init__(self):self.roles=[]
        def request(self,role,operation,data,response_type,*,validate,**kwargs):
            self.roles.append(role)
            if response_type is SourceTaskBatch:value=original
            else:
                value=dict(request_id=data['request_id'],proposal_kind='CARD_BATCH',proposal_hash=data['proposal_hash'],
                    outcome='REVISE',checked_anchor_ids=data['checked_anchor_ids'],checked_item_ids=data['checked_item_ids'],
                    issues=[dict(code='SOURCE_MISSTATEMENT',proposal_pointers=['/cards/1'],evidence_anchor_ids=['garbled'],
                        description='No readable source finding supports the extra draft assertion')],
                    corrected_proposal={'kind':'CARD_BATCH','payload':ready()},evidence_requests=[],unresolved_reason=None)
            result=response_type.model_validate(value);validate(result);return result
    ops=Correct();cards=selected_cards(ops,tile,binding,evidence)
    assert len(cards)==1 and cards[0].evidence_anchor_ids==['owned']
    assert ops.roles[-2:]==['SECONDARY','ARBITER']


@pytest.mark.parametrize('arbiter_outcome',['ACCEPT','SOURCE_UNRESOLVED'])
def test_unresolved_source_review_is_arbitrated_without_defaulting_to_pass(arbiter_outcome):
    tile,binding,evidence=inputs()
    class Uncertain(Script):
        def __init__(self):super().__init__();self.arbitrated=False
        def request(self,role,operation,data,response_type,*,validate,**kwargs):
            if response_type is SourceTaskBatch:
                value=response_type.model_validate(ready());validate(value);return value
            outcome='SOURCE_UNRESOLVED' if role=='SECONDARY' else arbiter_outcome
            if role=='ARBITER':
                self.arbitrated=True
                assert data['independent_review']['outcome']=='SOURCE_UNRESOLVED'
            value=SourceReviewResponse.model_validate(dict(request_id=data['request_id'],proposal_kind='CARD_BATCH',proposal_hash=data['proposal_hash'],
                outcome=outcome,checked_anchor_ids=data['checked_anchor_ids'],checked_item_ids=data['checked_item_ids'],issues=[],
                corrected_proposal=None,evidence_requests=[],unresolved_reason='A material source question remains' if outcome=='SOURCE_UNRESOLVED' else None))
            validate(value);return value
    ops=Uncertain()
    if arbiter_outcome=='ACCEPT':assert len(selected_cards(ops,tile,binding,evidence))==1
    else:
        with pytest.raises(Hold,match='Unresolved source-only'):selected_cards(ops,tile,binding,evidence)
    assert ops.arbitrated


@pytest.mark.parametrize('nested', [False, True])
def test_provider_schema_enforces_source_batch_outcome_contract(nested):
    import copy,jsonschema
    from validator.scientific_result_v1.source_tasks.response_schema import response_schema
    response_type=SourceReviewResponse if nested else SourceTaskBatch
    schema=response_schema(response_type,{'tile_id':'tile','checked_anchor_ids':['owned'],'checked_item_ids':['finding']})
    scoped={'$defs':schema['$defs'],'$ref':'#/$defs/SourceTaskBatch'} if nested else schema
    answer=SourceTaskBatch.model_validate(ready()).model_dump(mode='json')
    jsonschema.validate(answer,scoped)
    # The actual retained arbiter failure paired non-READY with authoritative cards.
    invalid=copy.deepcopy(answer);invalid['outcome']='NEED_EVIDENCE';invalid['evidence_requests']=[need()]
    with pytest.raises(jsonschema.ValidationError):jsonschema.validate(invalid,scoped)
    invalid['cards']=[];jsonschema.validate(invalid,scoped)
    invalid['outcome']='SOURCE_UNRESOLVED';invalid['evidence_requests']=[]
    with pytest.raises(jsonschema.ValidationError):jsonschema.validate(invalid,scoped)
    invalid['unresolved_reason']='The captured source cannot resolve this issue.'
    jsonschema.validate(invalid,scoped)
