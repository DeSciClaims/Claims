"""PDF byte binding, native table structure and replay without page crops."""
import copy
import json
import time

import pytest

from validator.scientific_result_v1.source_tasks.contracts import (
    ModelRoute,
    SourceDocument,
    SourcePacket,
)
from validator.scientific_result_v1.source_tasks.docling_frame import (
    validate_docling_frame,
)
from validator.scientific_result_v1.source_tasks.layout_validation import (
    validate_layout,
)
from validator.scientific_result_v1.source_tasks.operations import Operations
from validator.scientific_result_v1.source_tasks.preparation import prepare_paper
from validator.scientific_result_v1.source_tasks.store import Store, canonical, sha


def fixture(tmp_path,monkeypatch):
    store=Store(tmp_path/'store')
    raw=b'%PDF-1.4 independently supplied source fixture'
    pdf=store.put_named('sources/main.pdf',raw,'application/pdf')
    doc=SourceDocument(document_uid='doc',role='MAIN',title='Supplied paper',version=pdf.sha256,url=None,required=True,acquisition_status='SUPPLIED',availability='AVAILABLE',file=pdf)
    packet=SourcePacket(protocol_id='claims-source-task-v1',paper_id='P005',round_id='r',state='READY',documents=[doc],errors=[])
    def parse(path,output):
        assert path.read_bytes()==raw
        output.mkdir(parents=True,exist_ok=True)
        cells=[]
        for row,values in enumerate([['Outcome','Mean'],['Panel A',''],['A','1.2300'],['B','NA']]):
            for col,text in enumerate(values):
                if not text:continue
                cells.append(dict(text=text,start_row_offset_idx=row,end_row_offset_idx=row+1,
                    start_col_offset_idx=col,end_col_offset_idx=col+1,column_header=row==0,row_section=row==1,row_header=col==0))
        document={'body':{'children':[{'$ref':'#/texts/0'}]},'texts':[{'self_ref':'#/texts/0','label':'text','content_layer':'body','text':'In the experiment the sample contained 12 observations.','prov':[]}],
                  'tables':[{'self_ref':'#/tables/0','prov':[],'captions':[],'footnotes':[],
                             'data':{'num_rows':4,'num_cols':2,'table_cells':cells}}]}
        (output/'docling_document.json').write_text(json.dumps(document))
        (output/'source.md').write_text('Source text and the original table.')
        (output/'source_payload.json').write_text(json.dumps({'source_sha256':sha(raw),'parser':{'name':'docling','version':'test','ocr':True},'tables':[]}))
    monkeypatch.setattr('miner.scientific_result_v1.docling_source.parse_pdf',parse)
    def forbidden(*args,**kwargs):raise AssertionError('Docling mode must not call page-image code')
    for name in ['pdf_anchors','crop_anchor','split_page_anchor']:
        monkeypatch.setattr('validator.scientific_result_v1.source_tasks.parsing.'+name,forbidden)
    class Runtime:
        def generate(self,*,payload,stage,**kwargs):
            assert 'source_eligibility' in stage
            return {'items':[{'tile_id':t['tile_id'],'eligible':True,'reason':'Reported sample size'} for t in payload['tiles']]}
    routes={}
    for role,model in [('SOURCE_READER','p'),('PRIMARY','p'),('SECONDARY','s'),('ARBITER','a')]:
        routes[role]=ModelRoute(role=role,provider='script',model=model,runtime_revision='test',route_sha256='0'*64,
            supports_strict_json_schema=True,supports_images=False,context_window_tokens=100000,temperature=0,top_p=1,n=1,
            max_output_tokens=16384,request_timeout_seconds=120,provider_parameters=store.put({}))
    ops=Operations(store=store,routes=routes,runtimes={role:Runtime() for role in routes},tokenizers={role:lambda value:len(value.split()) for role in routes},trace_dir=tmp_path/'traces',deadline=time.time()+1800,policy_hash='1'*64)
    return store,packet,ops


def test_docling_parses_supplied_pdf_and_projects_native_rows_without_crops(tmp_path,monkeypatch):
    from validator.scientific_result_v1.source_tasks.contracts import (
        FileRef,
        SourceFrame,
    )
    store,packet,ops=fixture(tmp_path,monkeypatch)
    prepared,_=prepare_paper(store=store,packet=packet,parsed_directories={},operations=ops,stages=['week_one'],policy_sha256='1'*64)
    frame=store.load(FileRef.model_validate(prepared['source_frame'],strict=True),SourceFrame)
    assert all(anchor.rendering is None for anchor in frame.anchors)
    assert len(frame.tables)==1 and frame.tables[0].data_row_count==2
    rows=[a for a in frame.anchors if a.locator.kind=='table']
    assert rows[0].exact_text=='Panel A | A | 1.2300'
    assert rows[1].exact_text=='Panel A | B | NA'
    assert validate_docling_frame(store,frame,packet)
    validate_layout(store,frame.tables[0],frame,packet)
    # The source table has a parser proof, not fabricated model-review receipts.
    proof=json.loads(store.read(frame.tables[0].layout_receipt))
    assert proof['kind']=='claims_docling_table_layout_v1'
    assert 'source_reviews' not in proof
    changed=frame.model_copy(update={'anchors':[rows[0].model_copy(update={'exact_text':'A | 99'}),*frame.anchors[1:]]})
    with pytest.raises(ValueError,match='differs from retained'):
        validate_docling_frame(store,changed,packet)


def test_docling_eligibility_cannot_be_flipped_after_review(tmp_path,monkeypatch):
    from validator.scientific_result_v1.source_tasks.contracts import (
        FileRef,
        SourceFrame,
    )
    store,packet,ops=fixture(tmp_path,monkeypatch)
    prepared,_=prepare_paper(store=store,packet=packet,parsed_directories={},operations=ops,stages=['week_one'],policy_sha256='1'*64)
    frame=store.load(FileRef.model_validate(prepared['source_frame'],strict=True),SourceFrame)
    config=json.loads(store.get_blob(frame.parser_config_sha256))
    config['eligibility'][0]['eligible']=False
    ref=store.put(config)
    from validator.scientific_result_v1.source_tasks.docling_frame import derive_frame
    forged=derive_frame(store,packet,config,ref.sha256)
    with pytest.raises(ValueError,match='differ from retained decisions'):
        validate_docling_frame(store,forged,packet)


def test_miner_supplied_parser_cache_is_not_source_evidence(tmp_path,monkeypatch):
    from validator.scientific_result_v1.source_tasks.contracts import (
        FileRef,
        SourceFrame,
    )
    store,packet,ops=fixture(tmp_path,monkeypatch)
    untrusted=tmp_path/'miner-cache';untrusted.mkdir()
    (untrusted/'source_payload.json').write_text(json.dumps({'source_sha256':packet.documents[0].file.sha256}))
    (untrusted/'docling_document.json').write_text('{"fabricated":"miner says the mean is 99"}')
    prepared,_=prepare_paper(store=store,packet=packet,parsed_directories={'doc':untrusted},operations=ops,stages=['week_one'],policy_sha256='1'*64)
    frame=store.load(FileRef.model_validate(prepared['source_frame'],strict=True),SourceFrame)
    assert any('1.2300' in a.exact_text for a in frame.anchors)
    assert all('mean is 99' not in a.exact_text for a in frame.anchors)
    assert json.loads((untrusted/'docling_document.json').read_text())=={'fabricated':'miner says the mean is 99'}


def test_pdf_diagnostic_gate_failure_freezes_and_replays_without_scientific_calls(tmp_path,monkeypatch):
    from scripts.verify_scientific_example_papers import diagnostic_comparison
    from validator.scientific_result_v1.source_tasks.contracts import (
        FileRef,
        SourceFrame,
        SourceGridManifest,
        SubmittedPackage,
    )
    from validator.scientific_result_v1.source_tasks.evaluator import PaperEvaluator
    from validator.scientific_result_v1.source_tasks.replay import replay
    from validator.scientific_result_v1.structural import load_schema
    store,packet,ops=fixture(tmp_path,monkeypatch)
    prepared,_=prepare_paper(store=store,packet=packet,parsed_directories={},operations=ops,stages=['week_one'],policy_sha256='1'*64)
    frame=store.load(FileRef.model_validate(prepared['source_frame'],strict=True),SourceFrame)
    schema=load_schema();candidate=copy.deepcopy(next(v for v in schema['examples'] if v['paper']['id']=='P005'))
    candidate['results'][0]['closure']['core_ids']=['P005.E001.R001.F999']
    content=next(v['content'] for v in schema['x-table_extraction']['example_csv_files'] if v['extraction_prefix']=='P005.E001')
    package=SubmittedPackage(protocol_id=packet.protocol_id,paper_id=packet.paper_id,round_id=packet.round_id,
        output_alias='a',pipeline_digest='2'*64,roles=['BASELINE','BRONZE'],source_packet_sha256=sha(canonical(packet)),
        execution_receipt=store.put({'mode':'test'}),state='COMPLETED',extraction_json=store.put_named('a/output.json',json.dumps(candidate).encode(),'application/json'),
        table_csv_files=[store.put_named('a/P005.E001.T001.csv',content.encode(),'text/csv')],errors=[])
    grid=SourceGridManifest(protocol_id=packet.protocol_id,round_id=packet.round_id,stage='week_one',revision=0,
        precommitted_order_sha256='3'*64,initial_paper_ids=[packet.paper_id],replacements=[])
    plan,packages=diagnostic_comparison(store,packet,package,grid)
    seed=prepared['seeds_by_stage']['week_one']
    receipt,ref=PaperEvaluator(ops).evaluate(source_packet=packet,source_frame=frame,packages=packages,comparison_plan=plan,
        secret=store.read(FileRef.model_validate(seed['secret'],strict=True)),commitment=seed['commitment'],policy_sha256='1'*64,document_bindings={})
    assert receipt.state=='FROZEN'
    actual=next(score for score in receipt.paper_scores if score.output_alias=='a')
    assert actual.gate==0 and actual.score.numerator==0
    assert not receipt.scientific_verdicts
    assert replay(store,ref)==receipt


def test_tables_of_contents_are_classified_before_sampling_and_replay_is_complete(tmp_path,monkeypatch):
    from validator.scientific_result_v1.source_tasks.docling_frame import (
        prepare_docling_frame,
    )
    store,packet,ops=fixture(tmp_path,monkeypatch)
    original=__import__('miner.scientific_result_v1.docling_source',fromlist=['parse_pdf']).parse_pdf
    def parse(path,output):
        original(path,output)
        p=output/'docling_document.json';doc=json.loads(p.read_text());table=doc['tables'][0]
        table['captions']=[{'$ref':'#/texts/1'}]
        doc['texts'].append({'self_ref':'#/texts/1','label':'caption','text':'Table of contents','prov':[]})
        for cell in table['data']['table_cells']:
            if cell['text']=='1.2300':cell['text']='15'
        p.write_text(json.dumps(doc))
    monkeypatch.setattr('miner.scientific_result_v1.docling_source.parse_pdf',parse)
    for runtime in ops.runtimes.values():
        def generate(*,payload,**kwargs):
            return {'items':[{'tile_id':t['tile_id'],'eligible':t.get('source_kind')!='TABLE','reason':'Navigation page numbers, not measurements' if t.get('source_kind')=='TABLE' else 'Scientific prose'} for t in payload['tiles']]}
        runtime.generate=generate
    frame=prepare_docling_frame(store,packet,{},ops,'1'*64)
    assert not frame.tables and not any(t.stratum=='TABLE' for t in frame.tiles)
    assert any(a.anchor_id.startswith('d1-table0-row') for a in frame.anchors)
    assert any(a.anchor_id.startswith('d1-table0-row') for a in frame.anchors if a.anchor_id in frame.excluded_region_anchor_ids)
    assert validate_docling_frame(store,frame,packet)
    cfg=json.loads(store.get_blob(frame.parser_config_sha256));cfg['eligibility']=[d for d in cfg['eligibility'] if 'table' not in d['tile_id']]
    tampered=frame.model_copy(update={'parser_config_sha256':store.put(cfg).sha256})
    with pytest.raises(ValueError):validate_docling_frame(store,tampered,packet)
