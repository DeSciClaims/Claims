"""Full-source file access preserves targets and distinguishes task/candidate defects."""
from types import SimpleNamespace

import pytest
from tests.source_tasks.test_evidence_batching import case
from tests.source_tasks.test_source_scoring import generated  # noqa: F401
from validator.scientific_result_v1.source_tasks.evidence_batching import prepare
from validator.scientific_result_v1.source_tasks.contracts import JudgeResponse
from validator.scientific_result_v1.source_tasks.response_schema import response_schema
from validator.scientific_result_v1.source_tasks.transport import judge_packet
from validator.scientific_result_v1.source_tasks.judging import validate_judgment
from validator.scientific_result_v1.source_tasks.harness import validate_harness


def test_file_scientific_cases_get_all_captured_source_without_changing_owned_targets(generated,tmp_path,monkeypatch):
    _,_,ops,frame,task,packet=case(generated,tmp_path,monkeypatch)
    task=task.model_copy(update={'evidence_anchor_ids':['anchor-0']})
    ops.harness={'judge_source_corpus':'all_supplied_docling_anchors_v1'}
    [(child,view,evidence,lookup)]=prepare(ops,frame,[(task,packet)])
    assert child==task and lookup is None
    assert view.objects==packet.objects and view.relations==packet.relations and view.closures==packet.closures
    assert view.match==packet.match
    assert view.evidence_anchor_ids==[a.anchor_id for a in frame.anchors]
    assert evidence['anchors']==[a.model_dump(mode='json') for a in frame.anchors]


def test_fixed_audit_tasks_cannot_mislabel_candidate_errors_as_source_card_defects(generated,tmp_path,monkeypatch):
    _,_,_,frame,task,packet=case(generated,tmp_path,monkeypatch)
    task=task.model_copy(update={'task_kind':'OUTPUT_AUDIT','requirements':[]})
    data={'task':task.model_dump(mode='json'),'packet':judge_packet(packet),'required_dimensions':['REQUIRED_INFORMATION'],
          'authoritative_evidence':{'anchors':[a.model_dump(mode='json') for a in frame.anchors]}}
    schema=response_schema(JudgeResponse,data)
    assert 'TARGET_DEFECT' not in {b['properties']['outcome']['const'] for b in schema['anyOf']}
    response=SimpleNamespace(task_id=task.task_id,packet_id=packet.packet_id,outcome='TARGET_DEFECT')
    with pytest.raises(ValueError,match='defective generated CONTENT card'):
        validate_judgment(response,task,packet,set(),lambda ref:None)
