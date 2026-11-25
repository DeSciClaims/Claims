"""Re-evaluate the EXACT recorded testnet checks, preserving gates and denominators.

Reuses retained source preparation and mechanical decisions rather than paying
to regenerate different questions. Incremental cost is explicitly separate from
historical preparation cost. No backend access or award publication is possible.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
from collections import defaultdict
from pathlib import Path
import json
import os
import sys
import tempfile
import time

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from scripts.benchmark_budgeted_retrieval import ReadOnlyStore
from validator.scientific_result_v1.source_tasks.store import Store,canonical,sha
from validator.scientific_result_v1.source_tasks.contracts import (FileRef,SampledSilverReceipt,
    SourceFrame,TaskCard,AssertionPacket,SubmittedPackage,SampleManifest,FrozenSettings)
from validator.scientific_result_v1.source_tasks.configuration import freeze_settings
from validator.scientific_result_v1.source_tasks.runner import make_operations
from validator.scientific_result_v1.source_tasks.budgeted_validation import PROFILE
from validator.scientific_result_v1.source_tasks.failure_tolerance import policy,project,failure
from validator.scientific_result_v1.source_tasks.packages import Package
from validator.scientific_result_v1.source_tasks.packets import Index
from validator.scientific_result_v1.source_tasks.evaluator import evidence_for,expand_evidence
from validator.scientific_result_v1.source_tasks.evidence_batching import prepare
from validator.scientific_result_v1.source_tasks.sharding import partition
from validator.scientific_result_v1.source_tasks.judging import assess
from validator.scientific_result_v1.source_tasks.operations import retained_usage,Hold


class OverlayStore(Store):
    def __init__(self,root,original):
        self.original=original
        super().__init__(root)

    def get_blob(self,digest):
        try:return super().get_blob(digest)
        except KeyError:return self.original.get_blob(digest)


def main(argv=None,*,request_gate=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--evaluation',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--env-file',type=Path)
    parser.add_argument('--check-inputs-only',action='store_true')
    parser.add_argument('--runtime-controls',type=Path,help='Explicit recorded implementation-only amendment for resuming cached checks')
    args=parser.parse_args(argv)
    if args.env_file:
        from dotenv import dotenv_values
        for key,value in dotenv_values(args.env_file).items():
            if value and (key=='OPENROUTER_API_KEY' or key.startswith('CLAIMS_SCIENTIFIC_') and key.endswith('_API_KEY')):
                os.environ[key]=value
    original=ReadOnlyStore(args.evaluation/'store')
    baseline=json.loads((args.evaluation/'report.json').read_text())
    receipt=original.load(FileRef.model_validate(baseline['receipt'],strict=True),SampledSilverReceipt)
    frame=original.load(receipt.source_frame,SourceFrame)
    manifest=original.load(receipt.sampling_manifest,SampleManifest)
    original_events=baseline['failure_tolerance']['events']
    planned=[e for e in original_events if e['stage']=='SCIENTIFIC_CASE']
    if len({(e['output_alias'],e['key']) for e in planned})!=len(planned):
        raise ValueError('Original scientific-check ledger contains duplicate root checks')
    root=args.output.resolve();root.mkdir(parents=True,exist_ok=True)
    store=OverlayStore(root/'store',original); tolerance=policy('4/5',20)
    header=json.loads(next((args.evaluation.parent/'inputs.jsonl').open()))['header']
    models={role:header['models'][role] for role in ['SOURCE_READER','PRIMARY','SECONDARY','ARBITER']}
    execution=dict(mode='failure_tolerance_diagnostic',failure_tolerance_diagnostic=tolerance,
        budgeted_validation=dict(PROFILE),fixed_source_receipt=baseline['receipt'],
        scientific_accuracy_policy='unmeasured_without_truth_corpus')
    frozen=root/'run_inputs.json'
    if frozen.exists():
        saved=json.loads(frozen.read_text())
        if saved['source_receipt_sha256']!=baseline['receipt']['sha256']:raise ValueError('Original evaluation changed')
        settings=store.load(FileRef.model_validate(saved['settings'],strict=True),FrozenSettings)
        if json.loads(store.read(settings.execution_policy))['budgeted_validation']!=PROFILE:raise ValueError('Cost profile changed on resume')
        policy_hash=saved['policy_sha256']
    else:
        settings,ref=freeze_settings(store,route_snapshots=header['snapshots'],models=models,
            execution_policy=execution,sealed_evaluation_timeout_seconds=7200)
        policy_hash=sha(canonical(dict(settings=ref,source_receipt=baseline['receipt'])))
        frozen.write_text(json.dumps(dict(settings=ref.model_dump(mode='json'),policy_sha256=policy_hash,
            source_receipt_sha256=baseline['receipt']['sha256']),indent=2)+'\n')
    controls=dict(kind='claims_local_diagnostic_runtime_controls_v1',shared_max_llm_requests=500,
        max_provider_attempts=1,max_submissions_per_paper=4)
    if args.runtime_controls:
        controls=json.loads(args.runtime_controls.read_text())
    _,deadline=store.generation('fixed-check-comparison',policy_hash,timeout=7200)
    ops=make_operations(store=store,settings=settings,policy_sha256=policy_hash,trace_dir=root/'traces',
        deadline=deadline,offline=args.check_inputs_only,request_gate=request_gate,diagnostic_runtime_controls=controls)
    if args.check_inputs_only:
        print(json.dumps(dict(state='INPUTS_VERIFIED',paper_id=frame.paper_id,original_checks=len(planned))));return
    cards={t.task_id:t for ref in receipt.task_cards for t in [store.load(ref,TaskCard)]}
    packages={p.output_alias:p for ref in receipt.input_packages for p in [store.load(ref,SubmittedPackage)]}
    packets=defaultdict(dict);bindings={};table_map=defaultdict(dict)
    tiles={t.tile_id:t for t in frame.tiles}
    for ref in receipt.assertion_packets:
        packet=store.load(ref,AssertionPacket);packets[packet.output_alias][packet.task_id]=packet
        bindings.setdefault(packet.output_alias,packet.document_bindings)
        task=cards[packet.task_id]
        if task.task_kind=='ARCHIVE':
            uid=tiles[task.tile_ids[0]].table_uid
            for block in packet.csv_blocks:table_map[packet.output_alias][block.table_id]=uid
    buckets={(b.output_alias,b.bucket_id):b for b in manifest.output_buckets}
    baseline_bits={(e['output_alias'],e['key']):e for e in planned}
    events=[e for e in original_events if e['stage']!='SCIENTIFIC_CASE']
    outcomes=[]
    with tempfile.TemporaryDirectory(prefix='claims-fixed-checks-') as work:
        indexes={}
        for alias in sorted({e['output_alias'] for e in planned}):
            submitted=packages[alias];folder=Path(work)/alias;folder.mkdir()
            (folder/'output.json').write_bytes(store.read(submitted.extraction_json))
            for ref in submitted.table_csv_files:(folder/Path(ref.relative_path).name).write_bytes(store.read(ref))
            original_package=Package(folder,alias,paper_id=frame.paper_id)
            if original_package.gate.gate_bit!=1:raise ValueError('Previously gate-passing extraction changed')
            index=Index(original_package);indexes[alias]=index
            index.document_uid_by_source={b.source_id:b.document_uid for b in bindings.get(alias,[]) if b.state=='BOUND'}
            index.table_uid_by_output=table_map[alias]
        def check(event):
            local=ops.scoped();task=cards[event['key']];alias=event['output_alias'];index=indexes[alias]
            errors=[];bits=[];verdicts=[]
            try:
                if alias not in bindings:raise Hold('EVALUATOR','Original document binding was unresolved; no invented correspondence')
                owned=buckets.get((alias,task.source_bucket_id))
                owned_refs=owned.assertion_refs if owned else None
                if task.task_kind=='CONTENT':
                    match=index.match(task,local,evidence=evidence_for(frame,task.evidence_anchor_ids))
                    packet=index.packet(task,task.binding,match,sha(canonical(packages[alias])),bindings[alias])
                    shards=partition(index,task,packet,tokenizers=local.tokenizers,source_frame=frame)
                elif task.task_kind=='OUTPUT_AUDIT':
                    if owned_refs is None:raise Hold('EVALUATOR','Original complete audit ownership is unavailable')
                    match=index.audit_match(task,store,owned_refs)
                    packet=index.packet(task,task.binding,match,sha(canonical(packages[alias])),bindings[alias],owned_refs=owned_refs)
                    shards=partition(index,task,packet,tokenizers=local.tokenizers,source_frame=frame,owned_refs=owned_refs)
                else:
                    existing=packets[alias].get(task.task_id)
                    if existing is None:raise Hold('EVALUATOR','Original source-to-table correspondence is unresolved')
                    shards=[(task,existing)]
                for child,packet,evidence,lookup in prepare(local,frame,shards):
                    verdict=assess(local,task=child,packet=packet,evidence=evidence,source_lookup=lookup,
                        secret=bytes.fromhex(manifest.revealed_seed_hex),
                        scope=(task.binding.protocol_id,task.binding.round_id,task.binding.stage,task.binding.paper_id),
                        resolve=index.resolve,expand=lambda current,requests,limit:expand_evidence(frame,current,requests,limit,tokenizers=local.tokenizers))
                    verdicts.append(store.put(verdict).model_dump(mode='json'))
                    if verdict.state=='HOLD':errors.extend(verdict.hold_reasons)
                    else:bits.append(int(verdict.state=='PASS'))
            except Exception as exc:errors.append(failure(local,exc,'VERIFICATION'))
            if not bits and not errors:errors.append(failure(local,ValueError('No original scientific check assessed'),'VERIFICATION'))
            result={**event,'status':'UNRESOLVED' if errors else 'COMPLETED',
                'errors':[e.model_dump(mode='json') for e in errors],'bit':None if errors else min(bits),
                'verdicts':verdicts}
            ops.run_refs.extend(local.run_refs);ops.used.extend(local.used)
            old=baseline_bits[alias,task.task_id]
            result['baseline_status']=old['status'];result['baseline_bit']=old.get('bit')
            with (root/'case-progress.jsonl').open('a') as stream:stream.write(json.dumps(result)+'\n')
            return result
        with ThreadPoolExecutor(max_workers=20) as executor:
            for result in executor.map(check,planned):events.append(result);outcomes.append(result)
    projection=project(receipt=receipt,frame=frame,manifest=manifest,events=events,tolerance=tolerance)
    with store.connection() as c:
        runs=[FileRef(relative_path='blobs/'+digest,sha256=digest,byte_length=len(raw),media_type='application/octet-stream')
              for digest,raw in c.execute("select b.sha,b.data from heads h join blobs b on b.sha=h.sha where h.key like 'judge-run:%'")]
    usage=retained_usage(store,runs)
    comparable=[e for e in outcomes if e['status']==e['baseline_status']=='COMPLETED']
    report=dict(kind='budgeted_fixed_checks_comparison_v1',paper_id=frame.paper_id,competition_eligible=False,
        original_source_receipt=baseline['receipt'],profile=PROFILE,reused_source_and_mechanical_gates=True,
        source_preparation_cost_excluded=True,original_checks=len(planned),completed_checks=sum(e['status']=='COMPLETED' for e in outcomes),
        comparable_checks=len(comparable),agreement_checks=sum(e['bit']==e['baseline_bit'] for e in comparable),
        incremental_provider_confirmed_cost_usd=sum(v['cost_usd']or 0 for v in usage),
        unknown_billing_attempts=sum(v['cost_usd']is None for v in usage),scores=projection['scores'],
        stages=projection['stages'],events=outcomes,models=models)
    (root/'report.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps({k:v for k,v in report.items() if k not in {'scores','events','profile','original_source_receipt'}}),flush=True)
    return report


if __name__=='__main__':main()
