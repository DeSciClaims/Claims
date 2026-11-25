"""Readable per-stage rates, including recovered and unrecovered provider faults."""
from collections import Counter, defaultdict
import json

from .contracts import FileRef, JudgeRunReceipt
from .store import Store


def diagnostics(root):
    store=Store(root/'store')
    with store.connection() as connection:
        refs=[FileRef(relative_path='blobs/'+digest,sha256=digest,byte_length=len(raw),media_type='application/octet-stream')
              for digest,raw in connection.execute("select b.sha,b.data from heads h join blobs b on b.sha=h.sha where h.key like 'judge-run:%'")]
    logical=defaultdict(list);failure_types=Counter();attempt_errors=[]
    for ref in refs:
        run=store.load(ref,JudgeRunReceipt)
        request=json.loads(store.read(run.request))
        logical[(request['operation'],run.run_id.rsplit(':',1)[0])].append(run)
        if run.error:
            message=run.error.message.lower()
            category=('INVALID_RESPONSE' if any(word in message for word in ('validation','schema','json','pointer','truncat','length','echo','unknown','duplicate'))
                      else 'NETWORK_OR_PROVIDER' if any(word in message for word in ('timeout','timed out','connection','http','rate limit','429','402','502','503','504'))
                      else run.error.code)
            failure_types[category]+=1
            attempt_errors.append({'operation':request['operation'],'role':run.role,'run_id':run.run_id,
                                   'category':category,'error':run.error.model_dump(mode='json')})
    operations=defaultdict(lambda:{'completed':0,'failed':0,'attempts':0,'failed_attempts':0})
    for (name,_),runs in logical.items():
        record=operations[name];success=any(run.error is None and run.parsed_response is not None for run in runs)
        record['completed' if success else 'failed']+=1;record['attempts']+=len(runs)
        record['failed_attempts']+=sum(run.error is not None for run in runs)
    for record in operations.values():
        record['total']=record['completed']+record['failed']
        record['completion_fraction']=record['completed']/record['total']
    native=Counter();native_errors=[]
    for path in (root/'traces').rglob('inference_calls.jsonl'):
        for line in path.read_text().splitlines():
            event=json.loads(line);native[event['event']]+=1
            if event['event']=='request_error':
                native_errors.append({k:event.get(k) for k in ('call_id','stage','model','status_code','error_type','error_message')})
    return {'logical_operations':dict(sorted(operations.items())), 'failed_attempt_categories':dict(failure_types),
            'attempt_errors':attempt_errors,'native_events':dict(native),'native_errors':native_errors}


def write_report(root,report):
    projection=report['failure_tolerance'];detail=diagnostics(root)
    projection['operation_diagnostics']=detail
    (root/'failure-tolerance.json').write_text(json.dumps(projection,indent=2,ensure_ascii=False)+'\n')
    lines=['# Original validator with failure tolerance','',
           'Local diagnostic only. The original source tasks, matching, packet construction, scientific criteria, secondary review and arbitration remain in use. Production scoring and weights are unchanged.','',
           '**Acceptance threshold: '+projection['tolerance']['minimum_resolved_fraction']+' resolved weight separately in each applicable C/T/P component.** The conservative score gives no credit to unknown work while keeping it in its original denominator. This is a lower bound, not a measured scientific failure. Scores below the threshold are still shown, marked insufficiently evaluated.','',
           'A valid scientific FAIL counts as completed evaluation and earns zero scientific credit. Network/provider faults, invalid responses, timeouts, source ambiguity and other incomplete operations count as unresolved. Recovered failed attempts remain in the diagnostic ledger.','',
           '| Output | G | Conservative score | Upper bound | Coverage accepted |','|---|---:|---:|---:|---|']
    for score in projection['scores']:
        lines.append(f"| {score['output_alias']} | {score['gate']} | {score['conservative_score']:.4f} | {score['score_upper_bound']:.4f} | {score['coverage_accepted']} |")
    for score in projection['scores']:
        lines += ['', '## '+score['output_alias'],'','| Component | Lower | Upper | Resolved weight | Resolved / planned units | Threshold met |','|---|---:|---:|---:|---:|---|']
        for kind,value in (score.get('components') or {}).items():
            if value is None:continue
            lines.append(f"| {kind} | {value['lower']:.4f} | {value['upper']:.4f} | {value['resolved_weight_fraction']:.1%} | {value['resolved_units']} / {value['planned_units']} | {value['threshold_met']} |")
    lines += ['','## Logical operation completion','',
              'An operation that succeeds on retry is completed. Its earlier failed attempt still appears in the final two columns. Operation-level rates and weighted scientific coverage are different denominators.','',
              '| Operation | Completed | Terminal failures | Completion rate | Attempts | Failed attempts |','|---|---:|---:|---:|---:|---:|']
    for name,value in detail['logical_operations'].items():
        lines.append(f"| {name} | {value['completed']} | {value['failed']} | {value['completion_fraction']:.1%} | {value['attempts']} | {value['failed_attempts']} |")
    lines += ['','## Failure ledger','', 'Attempt categories: '+json.dumps(detail['failed_attempt_categories']), '',
              'Native provider events: '+json.dumps(detail['native_events']), '',
              'These are different views of retained attempts; do not add their counts together.','']
    for event in projection.get('events',[]):
        if event['status']=='COMPLETED':continue
        lines += ['- '+event['stage']+' '+event.get('key','')+': '+json.dumps(event.get('errors',event.get('error')),ensure_ascii=False)]
    lines += ['','## Cost and interpretation','',f"Recorded provider cost: ${report['provider_confirmed_cost_usd']:.6f}; unknown-billing attempts: {report['unknown_billing_attempts']}.",'',
              'Strict guide receipt state: '+report['state']+'. The partial-score projection is deliberately separate from official Q. Complete original evidence and attempted judgments remain in `store/` and `traces/`; per-operation and per-case errors are in [failure-tolerance.json](failure-tolerance.json).','',
              'This trial measures execution coverage and conservative score bounds, not scientific judge accuracy. A low conservative score can reflect evaluator failures rather than poor miner extraction.','']
    (root/'failure-tolerance.md').write_text('\n'.join(lines))
