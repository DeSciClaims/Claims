"""Source-only inventory/layout ownership before any miner output is supplied."""
import time
from validator.scientific_result_v1.source_tasks.contracts import SourcePacket,SourceDocument,SourceTableLayoutReceipt,SourceTableLayoutResponse,ModelRoute
from validator.scientific_result_v1.source_tasks.source import SourcePreparation,text_anchors,assemble_frame,validate_frame
from validator.scientific_result_v1.source_tasks.store import Store
from validator.scientific_result_v1.source_tasks.operations import Operations


def test_independent_inventory_and_layout_create_five_row_blocks(tmp_path):
    store=Store(tmp_path/'store')
    raw=b'Table 1. Marker concentrations.\nMarker A | 12.0 | 10.0\nMarker B | 3.50 | 4.10\n'
    ref=store.put_named('sources/source.txt',raw,'text/plain')
    doc=SourceDocument(document_uid='d1',role='MAIN',title='Synthetic source',version='1',url=None,required=True,acquisition_status='SUPPLIED',availability='AVAILABLE',file=ref)
    packet=SourcePacket(protocol_id='claims-source-task-v1',paper_id='P005',round_id='r1',state='READY',documents=[doc],errors=[])
    anchors=text_anchors(doc,raw);header=anchors[0].anchor_id
    rowids=[a.anchor_id for a in anchors if (a.exact_text or '').lstrip().startswith('Marker A') or (a.exact_text or '').lstrip().startswith('Marker B')]
    assert len(rowids)==2
    class SourceScript:
        def generate(self,*,payload,stage,**kwargs):
            assert 'packet' not in payload and 'submission' not in payload
            if stage.startswith('source-task-source_inventory_review') or stage.startswith('source-task-source_table_layout_review'):
                return dict(request_id=payload['request_id'],proposal_kind=payload['proposal_kind'],proposal_hash=payload['proposal_hash'],outcome='ACCEPT',checked_anchor_ids=payload['checked_anchor_ids'],checked_item_ids=payload['checked_item_ids'],issues=[],corrected_proposal=None,evidence_requests=[],unresolved_reason=None)
            if stage.startswith('source-task-source_inventory'):
                ids=[a['anchor_id'] for a in payload['anchors']]
                return dict(request_id=payload['request_id'],outcome='READY',inspected_anchor_ids=ids,regions=[dict(document_uid='d1',anchor_ids=ids,kind='TABLE',has_extractable_science=True,table_uid=None,proposed_table_key='table1',source_label='Table 1',source_regions=[dict(anchor_id=a,bbox=None) for a in ids],reason='Source numerical table')],companion_mentions=[],unresolved_reason=None)
            request=payload['request'];part=request['part_manifest'][0]['part_key']
            return dict(request_id=request['request_id'],table_uid=request['table_uid'],chunk_id=request['chunk_id'],requested_scope_anchor_ids=request['requested_scope_anchor_ids'],complete_for_scope=True,outcome='READY',inspected_anchor_ids=[a['anchor_id'] for a in payload['anchors']],layout=dict(document_uid='d1',identity_evidence_anchor_ids=[header],identity_note='Table 1',parts=[dict(part_key=part,anchor_ids=request['requested_scope_anchor_ids'],panel_label=None,panel_label_anchor_ids=[],continuation_of_part_key=None,column_keys=['c0001','c0002','c0003'])],columns=[dict(part_key=part,local_key='c000'+str(i),role='LABEL' if i==1 else 'DATA',header_path=[],description=None) for i in range(1,4)],rows=[dict(part_key=part,row_regions=[dict(anchor_id=a,bbox=None)],row_label_anchor_ids=[a],context_anchor_ids=[header]) for a in rowids],header_anchor_ids=[header],note_anchor_ids=[],excluded_regions=[]),evidence_requests=[],unresolved_reason=None)
    script=SourceScript();routes={}
    for role,model in [('SOURCE_READER','p'),('PRIMARY','p'),('SECONDARY','s'),('ARBITER','a')]:
        routes[role]=ModelRoute(role=role,provider='script',model=model,runtime_revision='test',route_sha256='0'*64,supports_strict_json_schema=True,supports_images=False,context_window_tokens=100000,temperature=0,top_p=1,n=1,max_output_tokens=16384,request_timeout_seconds=120,provider_parameters=store.put({}))
    ops=Operations(store=store,routes=routes,runtimes={r:script for r in routes},tokenizers={r:lambda s:len(s.split()) for r in routes},trace_dir=tmp_path/'trace',deadline=time.time()+1800,policy_hash='1'*64)
    preparation=SourcePreparation(ops,packet,anchors,'2'*64)
    inventory=preparation.inventory_page([a.anchor_id for a in anchors])
    from validator.scientific_result_v1.source_tasks.contracts import InventoryResponse
    from validator.scientific_result_v1.source_tasks.store import canonical
    # Controller assignment replaces the request-local discovery key with a verified UID.
    InventoryResponse.model_validate_json(canonical(inventory), strict=True)
    frame=assemble_frame(preparation,[inventory]);validate_frame(frame,packet)
    assert len(frame.tables)==1 and frame.tables[0].data_row_count==2
    assert len(frame.tiles)==1 and len(frame.tiles[0].owned_anchor_ids)==2
    layout=store.load(frame.tables[0].layout_receipt,SourceTableLayoutReceipt)
    assert layout.ordered_data_row_anchor_ids==frame.tables[0].data_row_anchor_ids
    assert all(store.load(f,SourceTableLayoutResponse).complete_for_scope for f in layout.chunk_responses)
