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

    def request(self, role, operation, data, response_type, *, validate, source_limit=32000):
        self.calls.append((role, operation, source_limit))
        assert data["controller_requirement_policy"]
        assert "Preserve each coherent parent card" in data["controller_requirement_policy"]
        assert "candidate" not in data and "packet" not in data
        if response_type is SourceTaskBatch:
            requested = self.always_requests or (len(self.calls) == 1 and not self.reviewer_requests)
            value = dict(tile_id='tile', outcome='NEED_EVIDENCE', cards=[],
                evidence_requests=[need()], unresolved_reason=None) if requested else ready(
                    'context' if self.wrong_owner else 'owned')
        else:
            assert response_type is SourceReviewResponse
            requested = self.reviewer_requests and len(self.calls) == 2
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
    assert len(expanded) == 1 and expanded[0][1] == 32000
    assert ops.calls[-1] == ('SECONDARY', 'source_card_review', 32000)


def test_source_card_context_expansion_is_bounded():
    tile, binding, evidence = inputs()
    ops = Script(always_requests=True)
    limits = []
    def expand(current, requests, limit):
        limits.append(limit)
        return current
    with pytest.raises(Hold, match='context remains unresolved'):
        selected_cards(ops, tile, binding, evidence, expand=expand)
    assert limits == [32000, 32000]
    assert [call[2] for call in ops.calls] == [32000, 32000, 32000]


def test_expanded_context_cannot_become_an_unrelated_owned_target():
    tile, binding, evidence = inputs()
    with pytest.raises(ValueError, match='original owned tile'):
        selected_cards(Script(wrong_owner=True), tile, binding, evidence,
            expand=lambda current, requests, limit: {'anchors': current['anchors'] + [
                {'anchor_id': 'context', 'exact_text': 'Unrelated finding.'}]})
