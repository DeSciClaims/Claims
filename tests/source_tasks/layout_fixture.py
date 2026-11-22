"""Complete retained source-layout wire fixture; contains no candidate output."""
from validator.scientific_result_v1.source_tasks.contracts import (
 SourceTableLayoutRequest,SourcePartIdentity,SourceTableLayoutResponse,SourceReviewResponse,SourceTableLayoutReceipt,
 SourceLogicalColumn,SourceColumnMember)
from validator.scientific_result_v1.source_tasks.store import canonical,sha


def layout_fixture(store,packet,row_ids,column_names):
    ids=['header',*row_ids]
    req=SourceTableLayoutRequest(request_id='fixture-layout',table_uid='t1',document_uid='d1',chunk_id='part0',
        requested_scope_anchor_ids=ids,context_anchor_ids=[],part_manifest=[SourcePartIdentity(part_key='part0',source_order=0,anchor_ids=ids)],split_depth=0)
    response=SourceTableLayoutResponse.model_validate(dict(request_id=req.request_id,table_uid='t1',chunk_id='part0',
        requested_scope_anchor_ids=ids,complete_for_scope=True,outcome='READY',inspected_anchor_ids=ids,
        layout=dict(document_uid='d1',identity_evidence_anchor_ids=['header'],identity_note='Fixture source table',
            parts=[dict(part_key='part0',anchor_ids=ids,panel_label=None,panel_label_anchor_ids=[],continuation_of_part_key=None,column_keys=['c'+str(i+1).zfill(4) for i in range(len(column_names))])],
            columns=[dict(part_key='part0',local_key='c'+str(i+1).zfill(4),role='LABEL' if i==0 else 'DATA',header_path=[dict(text=k,anchor_ids=['header'])],description=None) for i,k in enumerate(column_names)],
            rows=[dict(part_key='part0',row_regions=[dict(anchor_id=aid,bbox=None)],row_label_anchor_ids=[aid],context_anchor_ids=['header']) for aid in row_ids],
            header_anchor_ids=['header'],note_anchor_ids=[],excluded_regions=[]),evidence_requests=[],unresolved_reason=None),strict=True)
    review=SourceReviewResponse(request_id=req.request_id,proposal_kind='TABLE_LAYOUT',proposal_hash=sha(canonical(response)),outcome='ACCEPT',
        checked_anchor_ids=ids,checked_item_ids=['part0'],issues=[],corrected_proposal=None,evidence_requests=[],unresolved_reason=None)
    return store.put(SourceTableLayoutReceipt(table_uid='t1',document_uid='d1',source_packet_sha256=sha(canonical(packet)),
        chunk_requests=[store.put(req)],chunk_responses=[store.put(response)],source_reviews=[store.put(review)],arbiter_responses=[],
        ordered_data_row_anchor_ids=row_ids,logical_columns=[SourceLogicalColumn(column_uid=k,role='LABEL' if i==0 else 'DATA',header_path=[k],
            source_columns=[SourceColumnMember(part_key='part0',local_key='c'+str(i+1).zfill(4))]) for i,k in enumerate(column_names)],header_anchor_ids=['header'],note_anchor_ids=[]))
