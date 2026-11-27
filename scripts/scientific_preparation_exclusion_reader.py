"""Additive operator adapter for terminal, common preparation exclusions.

The frozen generator, original sample, completed scientific receipts and numeric
scores remain unchanged. No source frames or worker receipts are fabricated.
"""
import hashlib
import json
from urllib.parse import quote

KIND='claims_source_preparation_exclusion_v1'

def _get(backend,round_id):
    return backend._request('GET','/internal/competition/scientific/preparation-exclusions/'+quote(round_id,safe=''))

def _validate(row,raw,record):
    if hashlib.sha256(raw).hexdigest()!=row['receipt_sha256']:raise ValueError('Preparation exclusion hash mismatch')
    receipt=json.loads(raw)
    if (receipt.get('kind')!=KIND or
        (receipt.get('round_id'),receipt.get('stage'),receipt.get('paper_id'),receipt.get('configuration_sha256'))
        !=(record['round_id'],row['stage'],row['paper_id'],record['configuration_sha256']) or
        receipt.get('source_sha256')!=row['source_sha256'] or receipt.get('applies_to_all_entrants') is not True or
        receipt.get('score_measured') is not False or receipt.get('execution_attempted') is not False or
        receipt.get('reason')!='PREPARATION_DEADLINE_EXHAUSTED'):
        raise ValueError('Preparation exclusion scope or disposition mismatch')
    return receipt

def _pairs(row,pairs,entries,configuration):
    baseline=[e for e in entries if e['role'] in {'reference','incumbent'}]
    challengers={e['hotkey']:e for e in entries if e['role']=='challenger'}
    if len(baseline)!=1 or len(pairs)!=len(challengers) or {p['challenger_hotkey'] for p in pairs}!=set(challengers):
        raise ValueError('Preparation exclusion must cover every entrant')
    b=baseline[0]
    details={'protocol_id':'claims-source-task-v1','evaluation_status':'COMMON_EXCLUDED',
        'preparation_exclusion_sha256':row['receipt_sha256'],'scientific_score_measured':False}
    for p in pairs:
        if (p['paper_id']!=row['paper_id'] or p['stage']!=row['stage'] or
            p['configuration_sha256']!=configuration or
            p['challenger_package_sha256']!=challengers[p['challenger_hotkey']]['package_sha256'] or
            p['baseline_hotkey']!=b['hotkey'] or p['baseline_package_sha256']!=b['package_sha256'] or
            p['challenger_score']!=0 or p['baseline_score']!=0 or p['infrastructure_fault'] is not True or
            p['details_public']!=details):raise ValueError('Preparation exclusion pair changed')

def install(*,round_id,configuration_sha256):
    from competition import scientific_lineage,scientific_scoring,stage_controller,weekly_automation
    from competition import code_audit,worker_security_audit,pilot_controller_factory
    from competition.scientific_factory import ensure_stage_plan
    from validator.scientific_result_v1.source_tasks.store import canonical,sha
    original_verify=scientific_lineage.verify_current_grid
    original_advance=stage_controller.advance_ready_stage
    original_copy=scientific_scoring.scientific_copy_diagnostics

    def authorized(backend,rid,record=None):
        if rid!=round_id:return None
        record=record or backend.get_round(rid)
        spec=record['rules_public']['configuration'].get('scientific_scoring',{})
        if (record['configuration_sha256']!=configuration_sha256 or
            spec.get('first_round_scoring',{}).get('round_id')!=rid or
            spec.get('experimental_launch',{}).get('round_id')!=rid):
            raise ValueError('Preparation exclusion adapter requires its approved round and configuration')
        return record

    def verified(backend,rid,stage,record=None):
        record=authorized(backend,rid,record)
        if record is None:return {}
        rows={e['paper_id']:e for e in _get(backend,rid) if e['stage']==stage}
        sample={p['paper_id']:p for p in backend.list_sample(rid) if p['stage']==stage}
        with scientific_lineage.evidence_store(backend) as store:
            for pid,row in rows.items():
                if pid not in sample or sample[pid]['source_sha256']!=row['source_sha256']:
                    raise ValueError('Preparation exclusion differs from frozen source sample')
                receipt=_validate(row,store.get_blob(row['receipt_sha256']),record)
                clock_ref=receipt['preparation_clock'];raw=store.get_blob(clock_ref['sha256'])
                if len(raw)!=clock_ref['byte_length'] or sha(raw)!=clock_ref['sha256']:
                    raise ValueError('Preparation exclusion clock hash/length mismatch')
                clock=json.loads(raw)
                scope=sha(canonical([rid,pid,row['source_sha256'],configuration_sha256]))
                if clock['binding']!=scope or clock['deadline']>receipt['recorded_at_epoch']:
                    raise ValueError('Preparation exclusion precedes its bound deadline')
        return rows

    def verify_grid(rid,stage,*,backend,pairs,round_record=None):
        rows=verified(backend,rid,stage,round_record)
        if not rows:return original_verify(rid,stage,backend=backend,pairs=pairs,round_record=round_record)
        original_pairs=[p for p in pairs if p['paper_id'] not in rows]
        evidence=original_verify(rid,stage,backend=backend,pairs=original_pairs,round_record=round_record)
        entries=next(s['entries'] for s in backend.list_stages(rid) if s['stage']==stage)
        with scientific_lineage.evidence_store(backend) as store:
            head=store.head('scientific-plan:'+rid+':'+stage)
            if head is None:raise ValueError('Excluded scientific stage lacks current plan')
            current=json.loads(store.get_blob(head))['comparison_plan']['sha256']
        for pid,row in rows.items():
            actual=[p for p in pairs if p['paper_id']==pid]
            if not actual:continue  # Original callers independently require the full grid.
            _pairs(row,actual,entries,configuration_sha256)
            evidence.append({'paper_id':pid,'preparation_exclusion_sha256':row['receipt_sha256'],
                'comparison_plan_sha256':current})
        return sorted(evidence,key=lambda e:e['paper_id'])

    materialized=set()
    def advance(rid,*,backend,identity_preparer=None):
        record=authorized(backend,rid)
        if record is not None:
            ledger=backend.list_stages(rid)
            if ledger:
                active=ledger[-1]
                rows=verified(backend,rid,active['stage'],record)
                signature=(active['stage'],tuple(sorted(e['receipt_sha256'] for e in rows.values())))
                if rows and signature not in materialized:
                    stage_controller._enqueue_current_stage(rid,backend=backend,round_record=record,
                        receipt=active,sample=backend.list_sample(rid))
                    with scientific_lineage.evidence_store(backend) as store:
                        ensure_stage_plan(store,backend,rid,active['stage'])
                    backend._request('POST','/internal/competition/scientific/preparation-exclusions/'+
                        quote(rid,safe='')+'/'+quote(active['stage'],safe='')+'/materialize',{})
                    materialized.add(signature)
                if rows and active['stage'] in {'prescreen','screen'}:
                    state=backend.complete_scientific_stage(rid,active['stage'])
                    if state['status'] not in {'ready','pending','waiting_for_scientific_recovery'}:
                        return state
        return original_advance(rid,backend=backend,identity_preparer=identity_preparer)

    def copy(*,round_id: str,stage,entries,paper_ids,backend):
        rows=verified(backend,round_id,stage)
        return original_copy(round_id=round_id,stage=stage,entries=entries,
            paper_ids=[p for p in paper_ids if p not in rows],backend=backend)

    class AuditView:
        def __init__(self,backend,excluded):self.backend=backend;self.excluded=excluded
        def __getattr__(self,name):return getattr(self.backend,name)
        def list_sample(self,rid):
            return [p for p in self.backend.list_sample(rid) if (p['stage'],p['paper_id']) not in self.excluded]

    def audit_wrapper(original):
        def audit(rid,*,backend,**kwargs):
            if authorized(backend,rid) is None:return original(rid,backend=backend,**kwargs)
            stage=backend.list_stages(rid)[-1]['stage']
            rows=verified(backend,rid,stage)
            return original(rid,backend=AuditView(backend,{(stage,pid) for pid in rows}),**kwargs)
        return audit

    scientific_lineage.verify_current_grid=verify_grid
    scientific_scoring.scientific_copy_diagnostics=copy
    stage_controller.advance_ready_stage=advance
    weekly_automation.advance_ready_stage=advance
    code_reader=audit_wrapper(code_audit.collect_final_code_evidence)
    code_audit.collect_final_code_evidence=code_reader
    pilot_controller_factory.collect_final_code_evidence=code_reader
    security_reader=audit_wrapper(worker_security_audit.audit_final_worker_security)
    worker_security_audit.audit_final_worker_security=security_reader
    weekly_automation.audit_final_worker_security=security_reader
    return {'round_id':round_id,'configuration_sha256':configuration_sha256,
        'frozen_generator_preserved':True,'common_preparation_exclusion_reader':KIND}
