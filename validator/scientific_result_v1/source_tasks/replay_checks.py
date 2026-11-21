"""Cross-record checks that make a frozen receipt an auditable complete score grid."""
from collections import defaultdict
from .contracts import JudgeRunReceipt, JudgeResponse, SourceTaskBatch, SourceReviewResponse
from .sampling import selection_manifest, audit_buckets, selected_for_pass_audit
from .store import canonical, sha


def check_selections(store, receipt, source, manifest, selected, secret, scope):
    expected = []
    for name in ('content_prose', 'content_table', 'archive'):
        expected.append(selection_manifest(store, source.tiles, secret, scope, name, selected[name]))
    gates = {g.output_alias:g for g in receipt.deterministic_gates}
    grouped = defaultdict(list)
    for bucket in manifest.output_buckets:
        if bucket.output_alias not in gates or gates[bucket.output_alias].gate_bit != 1:
            raise ValueError('Audit bucket belongs to an unmeasured pipeline')
        grouped[bucket.output_alias].append(bucket.bucket_id)
    for alias, population in grouped.items():
        expected.append(selection_manifest(store, source.tiles, secret, scope, 'output_audit',
            audit_buckets(population, secret, scope), alias=alias, population=population))
    # Empty assertion populations still have a retained empty OUTPUT_AUDIT selection.
    for alias, gate in gates.items():
        if gate.gate_bit == 1 and alias not in grouped:
            expected.append(selection_manifest(store, source.tiles, secret, scope, 'output_audit', [], alias=alias, population=[]))
    key=lambda s:(s.stream,s.output_alias or '')
    if sorted(manifest.selections,key=key) != sorted(expected,key=key):
        raise ValueError('Missing, changed or duplicate selection/rank manifest')
    if (manifest.content_available,manifest.archive_available) != (
        any(t.eligible_content for t in source.tiles), any(t.numerical for t in source.tables)):
        raise ValueError('Scientific applicability changed')


def check_runs(store, receipt, cards, packets, verdicts, secret, scope):
    from .contracts import SourceFrame
    source=store.load(receipt.source_frame,SourceFrame)
    source_anchors={a.anchor_id:a.model_dump(mode='json') for a in source.anchors}
    runs={}; requests={}
    for ref in receipt.judge_runs:
        run=store.load(ref,JudgeRunReceipt)
        if run.run_id in runs:raise ValueError('Duplicate judge run identity')
        request=__import__('json').loads(store.read(run.request))
        key=run.run_id.rsplit(':',1)[0]
        original={k:v for k,v in request.items() if k!='validation_feedback'}
        if sha(canonical(original))!=key:raise ValueError('Judge request identity changed')
        if request['role']!=run.role or request['route']['role']!=run.role or request['route']['route_sha256']!=run.model_route_sha256:
            raise ValueError('Judge role/route mismatch')
        if request['policy_sha256']!=receipt.binding.policy_sha256:raise ValueError('Judge policy mismatch')
        if run.error:
            from .errors import validate_error
            validate_error(run.error)
        if run.parsed_response:store.read(run.parsed_response)
        if run.raw_response:store.read(run.raw_response)
        runs[run.run_id]=run; requests[run.run_id]=request
    accepted_batches={}
    for rid,run in runs.items():
        if run.error or run.parsed_response is None:continue
        req=requests[rid]
        if req['operation']=='source_card':
            batch=store.load(run.parsed_response,SourceTaskBatch)
            # NEED_EVIDENCE is a retained context request, not an authoritative
            # empty task batch. A later READY batch owns the source targets.
            if batch.outcome=='READY':accepted_batches[batch.tile_id]=batch
        if req['operation']=='source_card_reconcile':
            review=store.load(run.parsed_response,SourceReviewResponse)
            if review.outcome=='REVISE' and review.corrected_proposal is not None:
                batch=review.corrected_proposal.payload
                accepted_batches[batch.tile_id]=batch
    for tileid,batch in accepted_batches.items():
        expected={'task-'+sha(canonical([tileid,c])) for c in batch.cards}
        actual={c.task_id for c in cards.values() if c.task_kind=='CONTENT' and c.tile_ids==[tileid] and '::shard:' not in c.task_id}
        if actual!=expected:raise ValueError('Source cards dropped or invented after independent review')
        reviews=[store.load(r.parsed_response,SourceReviewResponse) for rid,r in runs.items()
            if not r.error and r.parsed_response and requests[rid]['operation'] in {'source_card_review','source_card_reconcile'}
            and requests[rid]['payload'].get('proposal',{}).get('tile_id')==tileid]
        if not any(r.outcome in {'ACCEPT','REVISE'} for r in reviews):raise ValueError('Source cards lack independent review')
    expected_batch_tiles={c.tile_ids[0] for c in cards.values() if c.task_kind=='CONTENT' and '::shard:' not in c.task_id}
    if set(accepted_batches)!=expected_batch_tiles:raise ValueError('Selected source cards lack complete generation evidence')
    for verdict in verdicts.values():
        if verdict.state=='HOLD':continue
        if not set(verdict.judge_run_ids)<=runs.keys():raise ValueError('Scientific verdict references nonexistent judge runs')
        case=packets[verdict.packet_sha256]
        applicable=[]
        for rid,run in runs.items():
            req=requests[rid];p=req['payload']
            if run.error or not run.parsed_response or p.get('packet',{}).get('packet_id')!=case.packet_id:continue
            if rid not in verdict.judge_run_ids:continue
            if req['operation'] not in {'primary_verify','secondary','arbitrate'}:continue
            from .transport import judge_packet, audit_packet, audit_evidence
            if p['packet']!=judge_packet(case) or audit_packet(p['packet'])!=case:
                raise ValueError('Judge transport omitted or changed original scientific assertions')
            evidence=audit_evidence(p['authoritative_evidence'])
            if any(a!=source_anchors.get(a['anchor_id']) for a in evidence['anchors']):
                raise ValueError('Judge transport changed the independently captured source evidence')
            applicable.append((run,req,store.load(run.parsed_response,JudgeResponse)))
        roles={r.role for r,_,_ in applicable}
        if 'PRIMARY' not in roles:raise ValueError('Scientific verdict lacks primary verification')
        primary=next(x for x in applicable if x[0].role=='PRIMARY')
        fingerprint=sha(canonical(primary[1]['payload']))
        response=primary[2]
        mandatory=(response.outcome!='ASSESSED' or any(c.status=='FAIL' for c in response.checks)
                   or selected_for_pass_audit(secret,scope,fingerprint))
        if mandatory and 'SECONDARY' not in roles:raise ValueError('Mandatory independent review missing')
        identities={(r.role,req['route']['provider'],req['route']['model'],req['route']['runtime_revision']) for r,req,_ in applicable}
        judge_ids=[i[1:] for i in identities]
        if len(judge_ids)!=len(set(judge_ids)):raise ValueError('Review model/runtime identities are not independent')
        for run,req,_ in applicable:
            if run.role=='SECONDARY' and any(k in req['payload'] for k in ('primary','verdict','rationale','secondary')):
                raise ValueError('Secondary review was not blind')
        if 'SECONDARY' in roles:
            second=next(x[2] for x in applicable if x[0].role=='SECONDARY')
            vector=lambda r:sorted((c.dimension,c.status,canonical(c.counterexamples)) for c in r.checks)
            if (response.outcome!=second.outcome or vector(response)!=vector(second)) and 'ARBITER' not in roles:
                raise ValueError('Scientific disagreement lacks arbitration')
        decisive=[r for r,_,_ in applicable if r.parsed_response.sha256==verdict.decisive_response_sha256]
        if not decisive or ('ARBITER' in roles and decisive[-1].role!='ARBITER'):
            raise ValueError('Decisive judgment does not follow independent review routing')
