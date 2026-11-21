"""Verify document/version correspondence from trusted inputs, never S-number equality."""
from .contracts import Closed,DocumentBinding
from .operations import Hold
from .budgets import SOURCE_TOKEN_BUDGETS
from .store import canonical,sha


class BindingResponse(Closed):
    request_id:str
    bindings:list[DocumentBinding]


def resolve_documents(ops,*,index,source_packet,source_frame):
    alias=index.alias;sources=index.package.payload['sources']
    anchors=[]
    for doc in source_packet.documents:
        if doc.file is None:continue
        candidates=[a for a in source_frame.anchors if a.document_uid==doc.document_uid]
        # The original first page and its captured text establish identity/version.
        pages=[a for a in candidates if a.locator.kind=='page' and a.locator.page_index==0]
        if pages:
            anchors.extend(pages)
        else:anchors.extend(candidates[:12])
    request_id='documents-'+sha(canonical([source_packet,sha(index.doc.raw),alias]))
    data={'request_id':request_id,'output_alias':alias,'source_manifest':source_packet.model_dump(mode='json'),
        'declared_sources':sources,'authoritative_evidence':{'anchors':[a.model_dump(mode='json') for a in anchors]}}
    anchor_documents={a.anchor_id:a.document_uid for a in source_frame.anchors}
    allowed=set(anchor_documents);docs={d.document_uid for d in source_packet.documents if d.file is not None}
    def validate(response):
        if response.request_id!=request_id:raise ValueError('Wrong document binding request')
        if len(response.bindings)!=len(sources) or {b.source_id for b in response.bindings}!={s['id'] for s in sources}:raise ValueError('Incomplete document/version correspondence')
        for b in response.bindings:
            if b.output_alias!=alias or not set(b.evidence_anchor_ids)<=allowed:raise ValueError('Invalid document binding witness')
            if b.state=='BOUND' and (b.document_uid not in docs or not b.evidence_anchor_ids or any(anchor_documents[a]!=b.document_uid for a in b.evidence_anchor_ids)):raise ValueError('Binding needs actual trusted input/version evidence')
    primary=ops.request('PRIMARY','source_document_binding',data,BindingResponse,validate=validate,source_limit=SOURCE_TOKEN_BUDGETS[-1])
    secondary=ops.request('SECONDARY','source_document_binding',data,BindingResponse,validate=validate,source_limit=SOURCE_TOKEN_BUDGETS[-1])
    vector=lambda r:sorted((b.source_id,b.state,b.document_uid) for b in r.bindings)
    final=secondary
    if vector(primary)!=vector(secondary):
        final=ops.request('ARBITER','source_document_binding',{**data,'primary':primary.model_dump(mode='json'),'secondary':secondary.model_dump(mode='json')},BindingResponse,validate=validate,source_limit=SOURCE_TOKEN_BUDGETS[-1])
    if any(b.state=='SOURCE_UNRESOLVED' for b in final.bindings):raise Hold('SOURCE','Document/version identity remains unresolved on supplied source evidence')
    return final.bindings
