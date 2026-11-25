"""Evaluate a trusted saved paper with a selectable frozen judge harness.

Input: validator-owned JSONL export (header plus content-addressed blobs), never a
miner-supplied parser cache. Original extraction bytes stay immutable. This local
driver does not connect to the competition backend or publish weights.
"""
import argparse
import base64
import json
import os
from pathlib import Path
import sys
import time

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from validator.scientific_result_v1.source_tasks.contracts import (FileRef,FrozenSettings,SourcePacket,
    SourceFrame,SubmittedPackage,SourceGridManifest,ComparisonPlan,SampledSilverReceipt)
from validator.scientific_result_v1.source_tasks.configuration import freeze_settings
from validator.scientific_result_v1.source_tasks.store import Store,sha,canonical
from validator.scientific_result_v1.source_tasks.runner import make_operations
from validator.scientific_result_v1.source_tasks.preparation import prepare_paper
from validator.scientific_result_v1.source_tasks.evaluator import PaperEvaluator
from validator.scientific_result_v1.source_tasks.replay import replay
from validator.scientific_result_v1.source_tasks.operations import retained_usage
from validator.scientific_result_v1.source_tasks.file_judge import _json


def select_models(original, overrides,diagnostic_profiles=None):
    from validator.scientific_result_v1.source_tasks.runner import APPROVED_MODELS
    selected = dict(original)
    seen = set()
    for assignment in overrides:
        role, separator, model = assignment.partition('=')
        if not separator or role not in selected or model not in APPROVED_MODELS|{v['model'] for v in (diagnostic_profiles or {}).values()}:
            raise ValueError('Use an approved model in ROLE=MODEL format')
        if role in seen:
            raise ValueError('A model role may only be overridden once')
        seen.add(role)
        selected[role] = model
    return selected


def main(argv=None, *, request_gate=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--bundle',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--harness',choices=['dspy','hermes-files'],default='dspy')
    p.add_argument('--hermes-python')
    p.add_argument('--model', action='append', default=[], metavar='ROLE=MODEL',
                   help='Freeze an approved model override; repeat for different roles')
    p.add_argument('--env-file',type=Path)
    p.add_argument('--diagnostic-profiles',type=Path)
    p.add_argument('--sampling-secret-file',type=Path)
    p.add_argument('--check-inputs-only',action='store_true')
    p.add_argument('--failure-tolerance',action='store_true',help='Local-only continuation and conservative partial-score report')
    p.add_argument('--minimum-resolved-fraction',default='1/2')
    p.add_argument('--diagnostic-workers',type=int,default=20)
    p.add_argument('--runtime-controls',type=Path,help='Owner-amended local diagnostic dispatch controls; requests and scientific policy stay bound')
    p.add_argument('--budgeted-validation',action='store_true',help='Local indexed retrieval and batched DSPy experiment; never competition qualified')
    args=p.parse_args(argv)
    budgeted=None
    if args.budgeted_validation:
        if not args.failure_tolerance or args.harness!='dspy':
            raise ValueError('Budgeted validation requires an explicit DSPy failure-tolerance diagnostic')
        from validator.scientific_result_v1.source_tasks.budgeted_validation import PROFILE
        budgeted=dict(PROFILE)
    controls=None
    if args.runtime_controls:
        if not args.failure_tolerance:raise ValueError('Runtime controls require an explicit local failure-tolerance diagnostic')
        from validator.scientific_result_v1.source_tasks.local_runtime_controls import validate
        controls=validate(json.loads(args.runtime_controls.read_text()))
    if args.env_file:
        from dotenv import dotenv_values
        # Only model credentials are needed; never load DB/wallet/controller keys.
        values=dotenv_values(args.env_file)
        for k,v in values.items():
            if v and (k=='OPENROUTER_API_KEY' or k.startswith('CLAIMS_SCIENTIFIC_') and k.endswith('_API_KEY')):os.environ[k]=v
    if args.hermes_python:os.environ['CLAIMS_SCIENTIFIC_HERMES_PYTHON']=args.hermes_python
    root=args.output.resolve();root.mkdir(parents=True,exist_ok=True);store=Store(root/'store')
    with args.bundle.open() as stream:
        header=json.loads(next(stream))['header']
        for line in stream:
            blob=json.loads(line);raw=base64.b64decode(blob['base64'],validate=True)
            if sha(raw)!=blob['sha256']:raise ValueError('Saved input blob hash differs')
            store.put_blob(blob['sha256'],raw)
    packet=store.load(FileRef.model_validate(header['refs']['source_packet'],strict=True),SourcePacket)
    profiles=json.loads(args.diagnostic_profiles.read_text()) if args.diagnostic_profiles else None
    models=select_models(header['models'],args.model,profiles)
    sampling_secrets={'week_one':args.sampling_secret_file.read_bytes()} if args.sampling_secret_file else None
    if sampling_secrets is not None and not (profiles or args.failure_tolerance):raise ValueError('Shared seeds are restricted to explicit local diagnostics')
    tolerance=None
    if args.failure_tolerance:
        if profiles:raise ValueError('This failure-tolerance trial uses the approved open-model routes, without proprietary profiles')
        from validator.scientific_result_v1.source_tasks.failure_tolerance import policy as tolerance_policy
        tolerance=tolerance_policy(args.minimum_resolved_fraction,args.diagnostic_workers)
    packages=[store.load(FileRef.model_validate(ref,strict=True),SubmittedPackage) for ref in header['refs']['input_packages']]
    baseline=[v.output_alias for v in packages if 'BASELINE' in v.roles]
    challengers=[v.output_alias for v in packages if 'CHALLENGER' in v.roles]
    reference=[v.output_alias for v in packages if 'BRONZE' in v.roles]
    if len(baseline)!=1 or len(reference)!=1 or not challengers:raise ValueError('Saved comparison roles are incomplete')
    frozen=root/'run_inputs.json'
    if frozen.exists():
        saved=json.loads(frozen.read_text())
        if (profiles or tolerance) and (saved.get('diagnostic_profiles_sha256')!=(sha(canonical(profiles)) if profiles else None) or saved.get('sampling_secret_sha256')!=(sha(sampling_secrets['week_one']) if sampling_secrets else None)):
            raise ValueError('Resume diagnostic profiles or sampling seed changed')
        if saved['bundle_sha256']!=sha(args.bundle.read_bytes()) or saved['harness']!=args.harness:
            raise ValueError('Resume inputs or harness changed; use a new diagnostic directory')
        settings=store.load(FileRef.model_validate(saved['settings'],strict=True),FrozenSettings)
        execution=json.loads(store.read(settings.execution_policy))
        if execution.get('budgeted_validation')!=budgeted:raise ValueError('Resume budgeted validation profile changed')
        if execution.get('failure_tolerance_diagnostic')!=tolerance:raise ValueError('Resume failure-tolerance policy changed')
        if {r.role:r.model for r in settings.model_routes} != models:
            raise ValueError('Resume model roles changed; use a new diagnostic directory')
        policy=saved['policy_sha256']
    else:
        execution={'mode':'local_proprietary_comparison_v1' if profiles else 'failure_tolerance_diagnostic' if tolerance else 'saved_paper_local_diagnostic',
                   'scientific_accuracy_policy':'unmeasured_without_truth_corpus'}
        if tolerance:execution['failure_tolerance_diagnostic']=tolerance
        if budgeted:execution['budgeted_validation']=budgeted
        settings,ref=freeze_settings(store,route_snapshots=header['snapshots'],models=models,diagnostic_profiles=profiles,
            harness=args.harness,hermes_python=args.hermes_python,sealed_evaluation_timeout_seconds=7200,
            execution_policy=execution)
        policy=sha(canonical({'settings':ref,'input_bundle_sha256':sha(args.bundle.read_bytes()),'harness':args.harness}))
        _json(frozen,{'settings':ref.model_dump(mode='json'),'policy_sha256':policy,'harness':args.harness,
                      'bundle_sha256':sha(args.bundle.read_bytes()),
                      'diagnostic_profiles_sha256':sha(canonical(profiles)) if profiles else None,
                      'sampling_secret_sha256':sha(sampling_secrets['week_one']) if sampling_secrets else None})
    directories={}
    for i,document in enumerate(header['parser_config']['documents']):
        directory=root/'retained-docling'/str(i);directory.mkdir(parents=True,exist_ok=True)
        for name,ref in document['artifacts'].items():
            if Path(name).is_absolute() or '..' in Path(name).parts:raise ValueError('Unsafe capture path')
            path=directory/name;path.parent.mkdir(parents=True,exist_ok=True)
            path.write_bytes(store.read(FileRef.model_validate(ref,strict=True)))
        directories[document['document_uid']]=directory
    if args.check_inputs_only:
        make_operations(store=store,settings=settings,policy_sha256=policy,trace_dir=root/'traces',offline=True,
                        diagnostic_runtime_controls=controls)
        print(json.dumps({'state':'INPUTS_VERIFIED','harness':args.harness,'packages':len(packages)}));return
    phase='source_preparation';report=None
    try:
        def operations(name):
            clock='local-phase:'+sha(canonical([policy,name]))
            _,deadline=store.generation(clock,policy,timeout=7200)
            return make_operations(store=store,settings=settings,policy_sha256=policy,trace_dir=root/'traces',deadline=deadline,
                                   request_gate=request_gate,diagnostic_runtime_controls=controls)
        prep=operations('source_preparation')
        print(json.dumps({'phase':phase,'paper_id':packet.paper_id,'harness':args.harness}),flush=True)
        prepared,_=prepare_paper(store=store,packet=packet,parsed_directories=directories,operations=prep,
                                  stages=['week_one'],policy_sha256=policy,sampling_secrets=sampling_secrets)
        frame=store.load(FileRef.model_validate(prepared['source_frame'],strict=True),SourceFrame)
        grid=SourceGridManifest(protocol_id=packet.protocol_id,round_id=packet.round_id,stage='week_one',revision=0,
            precommitted_order_sha256=sha(canonical([packet.paper_id])),initial_paper_ids=[packet.paper_id],replacements=[])
        plan=ComparisonPlan(protocol_id=packet.protocol_id,round_id=packet.round_id,stage='week_one',plan_revision=0,
            source_grid_revision=0,reference_fallback_used=False,baseline_alias=baseline[0],challenger_aliases=challengers,
            reference_alias=reference[0],source_grid=store.put(grid),parent_plan_sha256=None,reason='INITIAL')
        phase='scientific_verification';print(json.dumps({'phase':phase,'tiles':len(frame.tiles),'tables':len(frame.tables)}),flush=True)
        ops=operations(phase);seed=prepared['seeds_by_stage']['week_one']
        from validator.scientific_result_v1.source_tasks.recovery import reference as blob_reference
        from validator.scientific_result_v1.source_tasks.source_recovery import corrected_frame,correction_record
        head=store.head('paper:'+json.dumps([packet.round_id,'week_one',packet.paper_id]))
        ref=blob_reference(store,head) if head else None
        current=store.load(ref,SampledSilverReceipt) if ref else None
        if current is not None:frame=store.load(current.source_frame,SourceFrame)
        for _ in range(3):
            if current is not None and current.state=='FROZEN':break
            authorization=None;revision=current.binding.revision if current else 0
            if current is not None:
                proposal=store.head('source-correction-proposal:'+sha(canonical(current.binding)))
                if proposal is not None and len(current.corrections)<settings.maximum_common_corrections:
                    proof=blob_reference(store,proposal)
                    frame=corrected_frame(store,packet,frame,proof)
                    authorization={'predecessor':ref,'correction':correction_record(store,current,ref,proof)}
                    revision+=1
                    print(json.dumps({'phase':'common_source_correction','revision':revision,'predecessor':ref.sha256}),flush=True)
                elif not any(e.retryable for e in current.hold_reasons):break
            current,ref=PaperEvaluator(ops).evaluate(source_packet=packet,source_frame=frame,packages=packages,comparison_plan=plan,
                secret=store.read(FileRef.model_validate(seed['secret'],strict=True)),commitment=seed['commitment'],
                policy_sha256=policy,document_bindings={},revision=revision,generation_authorization=authorization)
            if current.state!='HOLD' or getattr(ops,'pending_source_correction',None) is None:break
        receipt=current
        replay_state='PASS'
        try:replay(store,ref)
        except Exception as exc:
            if not tolerance:raise
            replay_state='FAILED: '+type(exc).__name__+': '+str(exc)[:500]
        report={'state':receipt.state,'receipt':ref.model_dump(mode='json'),'offline_replay':replay_state,
                'scores':[v.model_dump(mode='json') for v in receipt.paper_scores],
                'hold_reasons':[v.model_dump(mode='json') for v in receipt.hold_reasons]}
        if tolerance:
            report['failure_tolerance']=json.loads(store.read(ops.failure_tolerance_projection))
    except Exception as exc:
        from validator.scientific_result_v1.source_tasks.errors import safe_message
        keys=[v for k,v in os.environ.items() if 'API_KEY' in k]
        report={'state':'ERROR','phase':phase,'error':safe_message(exc,keys)}
    if tolerance and 'failure_tolerance' not in report:
        report['failure_tolerance']={'kind':tolerance['kind'],'competition_eligible':False,'tolerance':tolerance,
            'scores':[{'output_alias':p.output_alias,'conservative_score':0.0,'score_upper_bound':1.0,
                       'coverage_accepted':False,'components':None,'gate':None,'official_q':None} for p in packages],
            'stages':{phase:{'completed':0,'unresolved':1,'total':1,'completion_fraction':0.0}},
            'events':[{'stage':phase,'status':'UNRESOLVED','error':report.get('error')}],
            'interpretation':'The common prerequisite failed. Zero is a conservative lower bound, not measured miner quality; no scientific coverage was established.'}
    with store.connection() as c:
        refs=[FileRef(relative_path='blobs/'+digest,sha256=digest,byte_length=len(raw),media_type='application/octet-stream')
              for digest,raw in c.execute("select b.sha,b.data from heads h join blobs b on b.sha=h.sha where h.key like 'judge-run:%'")]
    usage=retained_usage(store,refs)
    report.update(harness=args.harness,policy_sha256=policy,diagnostic_only=True,usage=usage,
                  provider_confirmed_cost_usd=sum(v['cost_usd'] or 0 for v in usage),
                  unknown_billing_attempts=sum(v['cost_usd'] is None for v in usage))
    if controls is not None:
        report['diagnostic_runtime_controls']=controls
        report['diagnostic_runtime_controls_sha256']=sha(canonical(controls))
    report_path=root/'report.json'
    if report_path.exists():
        old=report_path.read_bytes();history=root/'history';history.mkdir(exist_ok=True)
        (history/('report-'+sha(old)+'.json')).write_bytes(old)
    _json(report_path,report)
    if tolerance:
        from validator.scientific_result_v1.source_tasks.failure_tolerance_report import write_report
        write_report(root,report)
    print(json.dumps({k:v for k,v in report.items() if k not in {'usage','scores','hold_reasons'}},ensure_ascii=False),flush=True)
    if report['state']!='FROZEN' and not tolerance:raise SystemExit(1)
    return report


if __name__=='__main__':main()
