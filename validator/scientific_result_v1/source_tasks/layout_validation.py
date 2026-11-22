"""Verify the complete retained independent source-table layout proof."""
from .contracts import SourceTableLayoutReceipt,SourceTableLayoutRequest,SourceTableLayoutResponse,SourceReviewResponse
from .store import sha,canonical


def validate_layout(store,table,frame,packet):
    import json
    raw=json.loads(store.read(table.layout_receipt))
    if raw.get('kind')=='claims_docling_table_layout_v1':
        from .docling_frame import validate_docling_layout
        return validate_docling_layout(store,table,frame,packet)
    proof=store.load(table.layout_receipt,SourceTableLayoutReceipt)
    if (proof.table_uid,proof.document_uid,proof.source_packet_sha256)!=(table.table_uid,table.document_uid,sha(canonical(packet))):
        raise ValueError('Source table/layout proof binding changed')
    if (proof.ordered_data_row_anchor_ids,proof.header_anchor_ids,proof.note_anchor_ids)!= (table.data_row_anchor_ids,table.header_anchor_ids,table.note_anchor_ids):
        raise ValueError('Independent source rows/context changed')
    if [c.column_uid for c in proof.logical_columns]!=table.logical_column_names:
        raise ValueError('Source logical-column union changed')
    if len(proof.chunk_requests)!=len(proof.chunk_responses):raise ValueError('Incomplete table-layout scope coverage')
    known={a.anchor_id for a in frame.anchors};count=0
    reviews=[store.load(r,SourceReviewResponse) for r in proof.source_reviews]
    arbiters=[store.load(r,SourceReviewResponse) for r in proof.arbiter_responses]
    for request_ref,response_ref in zip(proof.chunk_requests,proof.chunk_responses,strict=True):
        request=store.load(request_ref,SourceTableLayoutRequest)
        response=store.load(response_ref,SourceTableLayoutResponse)
        if (response.request_id,response.table_uid,response.chunk_id,response.requested_scope_anchor_ids)!=(request.request_id,table.table_uid,request.chunk_id,request.requested_scope_anchor_ids):
            raise ValueError('Source table chunk identity changed')
        if not response.complete_for_scope or response.outcome!='READY':raise ValueError('Partial table layout was accepted')
        if set(response.inspected_anchor_ids)!=set(request.requested_scope_anchor_ids+request.context_anchor_ids) or not set(response.inspected_anchor_ids)<=known:
            raise ValueError('Table layout inspection coverage changed')
        count+=len(response.layout.rows)
        accepted=False
        for review in reviews:
            if review.request_id!=request.request_id or review.proposal_kind!='TABLE_LAYOUT':continue
            if review.outcome=='ACCEPT' and review.proposal_hash==response_ref.sha256:accepted=True
            if review.outcome=='REVISE':
                for arbiter in arbiters:
                    if arbiter.request_id!=request.request_id:continue
                    if arbiter.outcome=='ACCEPT' and arbiter.proposal_hash==response_ref.sha256:accepted=True
                    if arbiter.outcome=='REVISE' and arbiter.corrected_proposal is not None and arbiter.corrected_proposal.payload==response:accepted=True
        if not accepted:raise ValueError('Source table chunk lacks complete independent acceptance')
    if count!=table.data_row_count:raise ValueError('Source row count is not derived from complete accepted layouts')
    for aid in table.data_row_anchor_ids:
        anchor=next(a for a in frame.anchors if a.anchor_id==aid)
        if anchor.locator.kind!='table' or anchor.locator.table_uid!=table.table_uid:raise ValueError('Source row was attached to another table')
