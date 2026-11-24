import copy
from types import SimpleNamespace
import pytest
from tests.source_tasks.test_docling_frame import fixture
from validator.scientific_result_v1.source_tasks.contracts import SourceFrame,FileRef
from validator.scientific_result_v1.source_tasks.docling_frame import prepare_docling_frame
from validator.scientific_result_v1.source_tasks.operations import Hold
from validator.scientific_result_v1.source_tasks.table_count_review import verify_source_count,replay_source_count
from validator.scientific_result_v1.source_tasks.transport import audit_evidence


def setup(tmp_path,monkeypatch,uncertain=()):
    store,packet,ops=fixture(tmp_path,monkeypatch,context_window_tokens=262144)
    frame=prepare_docling_frame(store,packet,{},ops,ops.policy_hash)
    if isinstance(frame,tuple):frame=frame[0]
    if isinstance(frame,dict):frame=store.load(FileRef.model_validate(frame['source_frame']),SourceFrame)
    class Reviewer:
        def __init__(self,role):self.role=role
        def generate(self,*,payload,**kwargs):
            assert not any(k in payload for k in ['output_alias','candidate','csv_rows','candidate_count'])
            evidence=audit_evidence(payload['authoritative_evidence']);owned=payload['owned_data_row_anchor_ids']
            return {'request_id':payload['request_id'],'table_uid':payload['table_uid'],'source_frame_sha256':payload['source_frame_sha256'],
                'outcome':'SOURCE_UNRESOLVED' if self.role in uncertain else 'VERIFIED','verified_data_row_count':None if self.role in uncertain else len(owned),
                'inspected_anchor_ids':[a['anchor_id'] for a in evidence['anchors']],'evidence_anchor_ids':owned,'explanation':'Checked exact row boundaries against captured source.'}
    ops.runtimes={role:Reviewer(role) for role in ops.routes};ops.run_refs=[];ops.used=[]
    return store,frame,ops


def test_source_count_requires_independent_complete_witnesses_and_replays_without_model(tmp_path,monkeypatch):
    store,frame,ops=setup(tmp_path,monkeypatch)
    verify_source_count(ops,frame,frame.tables[0])
    receipt=SimpleNamespace(judge_runs=list(ops.run_refs),binding=SimpleNamespace(policy_sha256=ops.policy_hash))
    assert len(receipt.judge_runs)==2
    replay_source_count(store,receipt,frame,frame.tables[0])
    receipt.judge_runs=receipt.judge_runs[:1]
    with pytest.raises(ValueError,match='independent'):replay_source_count(store,receipt,frame,frame.tables[0])


def test_unresolved_source_layout_is_a_common_source_hold_not_a_miner_failure(tmp_path,monkeypatch):
    _,frame,ops=setup(tmp_path,monkeypatch,{'SECONDARY','ARBITER'})
    with pytest.raises(Hold,match='source table row boundaries') as exc:verify_source_count(ops,frame,frame.tables[0])
    assert exc.value.category=='SOURCE' and len(ops.run_refs)==3


def test_independent_arbiter_can_resolve_layout_disagreement_with_same_source(tmp_path,monkeypatch):
    store,frame,ops=setup(tmp_path,monkeypatch,{'PRIMARY'})
    verify_source_count(ops,frame,frame.tables[0])
    receipt=SimpleNamespace(judge_runs=ops.run_refs,binding=SimpleNamespace(policy_sha256=ops.policy_hash))
    replay_source_count(store,receipt,frame,frame.tables[0])


def test_retained_header_rows_keep_original_csv_coordinates_without_dropping_data():
    from validator.scientific_result_v1.source_tasks.packets import source_data_rows
    table={'columns':[{'name':'outcome','role':'label','header_path':['Outcome']},{'name':'mean','role':'data','header_path':['Group A (n = 53)','Mean']} ]}
    rows=[['outcome','mean'],['Outcome','Group A (n = 53) Mean'],['A','1.0'],['B','NA']]
    original=copy.deepcopy(rows)
    assert source_data_rows(table,rows,2,source_header_texts=['Outcome | Group A (n = 53)',' | Mean'])==[2,3]
    assert rows==original
    assert source_data_rows(table,rows,2) is None
    # Candidate metadata alone cannot label an extra numeric row as a source header.
    table['columns'][1]['header_path'].append('9.0');rows.append(['','9.0'])
    assert source_data_rows(table,rows,2,source_header_texts=['Outcome | Group A (n = 53)',' | Mean']) is None


def test_large_source_layout_is_reviewed_in_complete_nonoverlapping_batches(tmp_path,monkeypatch):
    import json
    store,frame,ops=setup(tmp_path,monkeypatch)
    def budget(text):
        try:value=json.loads(text)
        except ValueError:return len(text.split())
        return 36000*len(value['anchors']) if 'anchors' in value else len(text.split())
    ops.tokenizers={role:budget for role in ops.routes}
    verify_source_count(ops,frame,frame.tables[0])
    assert len(ops.run_refs)==4
    replay_source_count(store,SimpleNamespace(judge_runs=ops.run_refs,binding=SimpleNamespace(policy_sha256=ops.policy_hash)),frame,frame.tables[0])


def test_replay_rejects_a_verified_count_without_all_source_row_witnesses(tmp_path,monkeypatch):
    from validator.scientific_result_v1.source_tasks.contracts import JudgeRunReceipt
    from validator.scientific_result_v1.source_tasks.table_count_review import TableCountReview
    store,frame,ops=setup(tmp_path,monkeypatch);verify_source_count(ops,frame,frame.tables[0])
    run=store.load(ops.run_refs[0],JudgeRunReceipt);response=store.load(run.parsed_response,TableCountReview)
    altered=run.model_copy(update={'parsed_response':store.put(response.model_copy(update={'evidence_anchor_ids':[]}))})
    receipt=SimpleNamespace(judge_runs=[store.put(altered),*ops.run_refs[1:]],binding=SimpleNamespace(policy_sha256=ops.policy_hash))
    with pytest.raises(ValueError,match='witness every'):replay_source_count(store,receipt,frame,frame.tables[0])
