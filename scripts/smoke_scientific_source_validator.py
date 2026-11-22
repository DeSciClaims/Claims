"""Live source-task integration check on the guide's synthetic P005 table fixture.

This is not a paper benchmark or an estimate of scientific judge accuracy.
"""
import argparse
import copy
import json
from pathlib import Path
import time
from dotenv import load_dotenv
from validator.scientific_result_v1.source_tasks.contracts import (SourcePacket,SourceDocument,SubmittedPackage,ComparisonPlan,SourceGridManifest)
from validator.scientific_result_v1.source_tasks.store import Store,canonical,sha
from validator.scientific_result_v1.source_tasks.configuration import freeze_settings,MODELS
from validator.scientific_result_v1.source_tasks.runner import make_operations
from validator.scientific_result_v1.source_tasks.preparation import prepare_paper
from validator.scientific_result_v1.source_tasks.evaluator import PaperEvaluator
from validator.scientific_result_v1.source_tasks.replay import replay
from validator.scientific_result_v1.source_tasks.operations import retained_usage,Hold
from validator.scientific_result_v1.structural import load_schema


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir',type=Path,required=True)
    parser.add_argument('--snapshots',type=Path,required=True)
    parser.add_argument('--env-file',type=Path)
    args=parser.parse_args()
    if args.env_file:load_dotenv(args.env_file)
    root=args.output_dir.resolve();root.mkdir(parents=True,exist_ok=True);store=Store(root/'store')
    snapshots={}
    for model in set(MODELS.values()):
        value=json.loads((args.snapshots/(model.replace('/','--')+'.json')).read_text())
        snapshots[model]=value.get('data',value)
    settings,settings_ref=freeze_settings(store,route_snapshots=snapshots,execution_policy={'mode':'synthetic_integration_check','scientific_accuracy_policy':'unmeasured_without_truth_corpus'})
    policy=sha(canonical({'settings':settings_ref,'source_scope_policy':'supplied_sources_only','scientific_accuracy_policy':'unmeasured_without_truth_corpus'}))
    def ops(offline=False,prepare=False):return make_operations(store=store,settings=settings,policy_sha256=policy,
        trace_dir=root/'traces',deadline=time.time()+(7200 if prepare else 1800),offline=offline)
    schema=load_schema();example=next(p for p in schema['examples'] if p['paper']['id']=='P005')
    # Literal source fixture text and display strings supplied with the schema example.
    csv='outcome,group_a_mean_se,group_b_mean_se,p_value\nMarker A (mg/L),12.0 ± 1.2,10.0 ± 1.1,0.04*\nMarker B (ng/mL),3.50 ± 0.20,4.10 ± 0.30,<0.001*\n'
    # A single displayed source table; quoted result snippets must not duplicate it.
    import csv as csv_module
    from io import StringIO
    display='\n'.join(' | '.join(row) for row in csv_module.reader(StringIO(csv)))
    text='Table 1. Marker concentrations in groups A and B; one synthetic descriptive noncausal investigation.\n'+display+'\n'
    text+='\n'.join(a['text'] for a in example['tables'][0]['annotations'] if 'text' in a)+'\n'
    source=store.put_named('sources/main.txt',text.encode(),'text/plain')
    document=SourceDocument(document_uid='doc-'+source.sha256,role='MAIN',title='Synthetic P005 source',version=source.sha256,url=None,required=True,acquisition_status='SUPPLIED',availability='AVAILABLE',file=source)
    packet=SourcePacket(protocol_id='claims-source-task-v1',paper_id='P005',round_id='synthetic-p005',state='READY',documents=[document],errors=[])
    preparation=ops(prepare=True)
    evaluation=None
    try:
        print('Preparing independent source inventory and table layout.',flush=True)
        prepared,_=prepare_paper(store=store,packet=packet,parsed_directories={},operations=preparation,stages=['week_one'],policy_sha256=policy)
        from validator.scientific_result_v1.source_tasks.contracts import SourceFrame,FileRef
        frame=store.load(FileRef.model_validate(prepared['source_frame'],strict=True),SourceFrame)
        grid=SourceGridManifest(protocol_id='claims-source-task-v1',round_id=packet.round_id,stage='week_one',revision=0,precommitted_order_sha256=sha(canonical(['P005'])),initial_paper_ids=['P005'],replacements=[])
        plan=ComparisonPlan(protocol_id='claims-source-task-v1',round_id=packet.round_id,stage='week_one',plan_revision=0,source_grid_revision=0,reference_fallback_used=False,baseline_alias='a',challenger_aliases=['b'],reference_alias='a',source_grid=store.put(grid),parent_plan_sha256=None,reason='INITIAL')
        packages=[]
        for alias in ('a','b'):
            candidate=copy.deepcopy(example)
            if alias=='b':candidate['results'][1]['fields'][0]['value']=35
            packages.append(SubmittedPackage(protocol_id=packet.protocol_id,paper_id='P005',round_id=packet.round_id,output_alias=alias,pipeline_digest=sha(alias.encode()),roles=['BASELINE','BRONZE'] if alias=='a' else ['CHALLENGER'],source_packet_sha256=sha(canonical(packet)),execution_receipt=store.put({'status':'completed','fixture':True}),state='COMPLETED',extraction_json=store.put_named(alias+'/output.json',canonical(candidate),'application/json'),table_csv_files=[store.put_named(alias+'/P005.E001.T001.csv',csv.encode(),'text/csv')],errors=[]))
        seed=prepared['seeds_by_stage']['week_one'];secret=store.read(FileRef.model_validate(seed['secret'],strict=True))
        evaluation=ops()
        print('Evaluating sealed original packages against common source tasks.',flush=True)
        receipt,ref=PaperEvaluator(evaluation).evaluate(source_packet=packet,source_frame=frame,packages=packages,comparison_plan=plan,
            secret=secret,commitment=seed['commitment'],policy_sha256=policy,document_bindings={})
        replay(store,ref)
        report={'state':receipt.state,'receipt':ref.model_dump(mode='json'),'scores':[v.model_dump(mode='json') for v in receipt.paper_scores],
            'hold_reasons':[r.model_dump(mode='json') for r in receipt.hold_reasons],'source_preparation_usage':retained_usage(store,preparation.run_refs),'evaluation_usage':retained_usage(store,receipt.judge_runs),
            'source_inventory':{'table_count':len(frame.tables),'table_rows':[t.data_row_count for t in frame.tables]},
            'offline_replay':'passed','scientific_accuracy':'unmeasured; synthetic integration check only'}
    except Hold as exc:
        report={'state':'HOLD','category':exc.category,'reason':str(exc),'source_preparation_usage':retained_usage(store,preparation.run_refs),'evaluation_usage':retained_usage(store,evaluation.run_refs) if evaluation else [],'scientific_accuracy':'unmeasured'}
    scores={s['output_alias']:s for s in report.get('scores',[])}
    def number(value):return value['numerator']/value['denominator'] if value else None
    report['integration_passed']=(report['state']=='FROZEN' and report.get('offline_replay')=='passed'
        and report.get('source_inventory')=={'table_count':1,'table_rows':[2]}
        and set(scores)=={'a','b'} and all(s['gate']==1 for s in scores.values())
        and number(scores['a']['score'])==1 and number(scores['b']['score']) is not None
        and number(scores['b']['score'])<1)
    (root/'report.json').write_text(json.dumps(report,indent=2))
    print(json.dumps({'state':report['state'],'integration_passed':report['integration_passed'],'report':str(root/'report.json')}),flush=True)
    if not report['integration_passed']:raise SystemExit(1)

if __name__=='__main__':main()
