"""Audited recovery runner: sealed outputs, original seed, isolated paper checkpoints."""
from __future__ import annotations

import json
import time
import threading
import re
import sqlite3
from pathlib import Path
from urllib.parse import quote

from .contracts import (
    ComparisonPlan,
    FileRef,
    SampledSilverReceipt,
    SourceFrame,
    SourcePacket,
    SubmittedPackage,
)
from .remote import RemoteStore
from .evidence_transport import evidence_request
from .store import Conflict, Store, canonical, sha


# PDF conversion has a much larger memory footprint than text-only judging.
# Ten paper reviewers may overlap while at most two converters prepare sources.
PARSER_GATE=threading.BoundedSemaphore(2)


def prepare_recovery_frame(*args,**kwargs):
    from .docling_frame import prepare_docling_frame
    with PARSER_GATE:return prepare_docling_frame(*args,**kwargs)


class RecoveryStore(RemoteStore):
    """Blobs and exact request caches are shared; mutable paper generations are isolated."""
    def __init__(self,root,backend,manifest):
        self.manifest=manifest
        self.run_id=manifest['run_id']
        self.shared=Store(root)
        retained=(manifest.get('configuration',{}).get('scientific_scoring',{}).get('recovery',{}).get('source_parser',{}).get('retained_source_run_id'))
        if retained is not None and (not isinstance(retained,str) or not re.fullmatch(r'[a-f0-9]{32}',retained)):
            raise ValueError('Invalid retained source cache identity')
        self.capture_cache=Path(root)/'recoveries'/retained/'evidence.sqlite3' if retained else None
        super().__init__(Path(root)/'recoveries'/self.run_id,backend)

    def get_blob(self,expected):
        try:return Store.get_blob(self,expected)
        except KeyError:
            try:raw=self.shared.get_blob(expected)
            except KeyError:
                raw=None
                if self.capture_cache is not None and self.capture_cache.is_file():
                    with sqlite3.connect('file:'+str(self.capture_cache)+'?mode=ro',uri=True) as c:
                        row=c.execute('select data from blobs where sha=?',(expected,)).fetchone()
                    if row is not None:
                        raw=bytes(row[0])
                        if sha(raw)!=expected:raise ValueError('Corrupt retained source cache blob')
                if raw is None:return super().get_blob(expected)
            Store.put_blob(self,expected,raw)
            return raw

    get_receipt=get_blob

    def _key(self,key):
        # Exact immutable request identities may reuse work across sessions. The
        # request hash includes policy, route, prompt, schema and complete payload.
        if key.startswith(('judge-run:','provider-usage:','model-operation:','model-completion:')):return key
        return 'recovery:'+self.run_id+':'+key

    def head(self,key):
        return evidence_request(self.backend,'GET','/internal/competition/scientific/heads/'+quote(self._key(key),safe=''))['sha256']

    def advance(self,key,parent,child):
        response=evidence_request(self.backend,'POST','/internal/competition/scientific/heads/'+quote(self._key(key),safe=''),
            {'expected_parent_hash':parent,'new_hash':child})
        if response!={'sha256':child}:raise Conflict('Recovery head changed')
        return child

    def advance_paper_revision(self,round_id,stage,paper_id,expected_parent_hash,new_hash):
        raw=self.get_blob(new_hash)
        receipt=SampledSilverReceipt.model_validate_json(raw,strict=True)
        if receipt.state=='FROZEN':
            from .replay import replay
            replay(self,FileRef(relative_path='blobs/'+new_hash,sha256=new_hash,byte_length=len(raw),media_type='application/octet-stream'))
        path='/internal/competition/scientific/recoveries/'+round_id+'/'+self.run_id+'/papers/'+stage+'/'+quote(paper_id,safe='')
        response=evidence_request(self.backend,'POST',path,{'expected_parent_hash':expected_parent_hash,'new_hash':new_hash})
        if response!={'sha256':new_hash}:raise Conflict('Recovery paper checkpoint changed')
        return new_hash


def reference(store,digest):
    raw=store.get_blob(digest)
    return FileRef(relative_path='blobs/'+digest,sha256=digest,byte_length=len(raw),media_type='application/octet-stream')


def phase_deadline(store,manifest,paper,name,timeout):
    if name not in {'source_preparation','scientific_verification'}:raise ValueError('Unknown recovery phase')
    key='recovery-phase:'+sha(canonical([manifest['run_id'],paper['stage'],paper['paper_id'],name]))
    binding=sha(canonical([manifest['configuration_sha256'],paper['input_pool_sha256'],paper['source_packet_sha256']]))
    return store.generation(key,binding,timeout=timeout)[1]


def parser_version():
    from importlib.metadata import version
    return version('docling')


def capture_configuration(store,manifest,paper,original,packet):
    """Reuse only hash-bound independent parser bytes, never old source judgments."""
    options=manifest['configuration']['scientific_scoring']['recovery'].get('source_parser',{})
    frame=store.load(original.source_frame,SourceFrame)
    fallback=json.loads(store.get_blob(frame.parser_config_sha256))
    retained=options.get('retained_capture_frames',{}).get(paper['paper_id'])
    if retained is None:return fallback
    frame=store.load(FileRef.model_validate(retained['frame'],strict=True),SourceFrame)
    config=json.loads(store.get_blob(frame.parser_config_sha256))
    origin=json.loads(store.read(FileRef.model_validate(retained['configuration'],strict=True)))
    if sha(canonical(origin))!=config['policy_sha256']:
        raise ValueError('Retained Docling capture has a different origin policy')
    if frame.source_packet_sha256!=sha(canonical(packet)):
        raise ValueError('Retained Docling capture belongs to another source packet')
    prior_settings=json.loads(store.read(FileRef.model_validate(origin['scientific_scoring']['settings'],strict=True)))
    from .identity import runtime_descriptor
    if prior_settings['source_parser_digest']!=runtime_descriptor()['source_parser_digest']:
        return fallback
    scale=options.get('ocr_scale',3.0)
    if any(d['parser'].get('ocr_scale',3.0)!=scale or d['parser'].get('version')!=parser_version()
           for d in config['documents']):return fallback
    from .docling_frame import validate_docling_frame
    if not validate_docling_frame(store,frame,packet):
        raise ValueError('Retained capture lacks independent Docling proof')
    return config


def evaluate_paper(*,store,manifest,paper,settings,offline=False):
    """Same immutable packages, comparison grid and secret; no miner execution path."""
    from .evaluator import PaperEvaluator
    from .runner import make_operations
    from .sampling import PROTOCOL, seed_commitment
    from .source_recovery import corrected_frame, correction_record
    if paper['action']=='RETAIN':return {'paper_id':paper['paper_id'],'status':'retained'}
    path_id=sha(canonical([paper['stage'],paper['paper_id']]))
    original=store.load(reference(store,paper['original_receipt_sha256']),SampledSilverReceipt)
    packet=store.load(original.source_packet,SourcePacket)
    packages=[store.load(ref,SubmittedPackage) for ref in original.input_packages]
    plan=store.load(original.comparison_plan,ComparisonPlan)
    old_prepared=json.loads(store.get_blob(paper['original_prepared_source_sha256']))
    old_seed=old_prepared['seeds_by_stage'][paper['stage']]
    secret=store.read(FileRef.model_validate(old_seed['secret'],strict=True))
    policy=manifest['configuration_sha256'];scope=(PROTOCOL,packet.round_id,paper['stage'],packet.paper_id)
    commitment=seed_commitment(secret,scope,sha(canonical(packet)),policy)
    phase_key='progress:'+paper['stage']+':'+paper['paper_id']
    def phase(name,**extra):
        value={'phase':name,'updated_at':time.time(),**extra}
        previous=store.head(phase_key)
        store.advance(phase_key,previous,store.put(value).sha256)
    phase('source_preparation')
    def operations_for_phase(name):
        deadline=phase_deadline(store,manifest,paper,name,settings.sealed_evaluation_timeout_seconds)
        configured=make_operations(store=store,settings=settings,policy_sha256=policy,trace_dir=store.root/'traces'/path_id,deadline=deadline,offline=offline)
        configured.runtimes={role:BudgetedRuntime(runtime,budget) for role,runtime in configured.runtimes.items()}
        return configured
    from .billing import BudgetedRuntime, RecoveryBudget
    budget_policy=manifest['configuration']['scientific_scoring']['recovery']['diagnostic_budget']
    budget=RecoveryBudget(store.root/'budget.sqlite3',limit_usd=budget_policy['limit_usd'],reserve_usd=budget_policy['per_request_reserve_usd'])
    ops=operations_for_phase('source_preparation')
    parser=manifest['configuration']['scientific_scoring']['recovery'].get('source_parser',{})
    ops.ocr_scale=parser.get('ocr_scale',3.0)
    current_head=store.head('paper:'+json.dumps([packet.round_id,paper['stage'],packet.paper_id]))
    current=store.load(reference(store,current_head),SampledSilverReceipt) if current_head else None
    if current is not None and current.state=='FROZEN':
        from .replay import replay
        replay(store,reference(store,current_head));return {'paper_id':paper['paper_id'],'status':'scored','receipt_sha256':current_head}
    frame_head=store.head('recovery-frame:'+paper['paper_id'])
    if frame_head:
        frame=store.load(reference(store,frame_head),SourceFrame)
    else:
        directories={}
        config=capture_configuration(store,manifest,paper,original,packet)
        for number,spec in enumerate(config['documents']):
            directory=store.root/'retained-docling'/path_id/str(number);directory.mkdir(parents=True,exist_ok=True)
            for name,ref in spec['artifacts'].items():
                path=directory/name
                if (path.is_absolute() and directory not in path.parents) or '..' in Path(name).parts:raise ValueError('Unsafe retained parser path')
                path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(store.read(FileRef.model_validate(ref,strict=True)))
            (directory/'validator_parse_run.json').write_text(json.dumps({'independent_validator_parse':True,'source_sha256':spec['source_sha256'],
                'ocr_scale':spec['parser'].get('ocr_scale',3.0)}))
            directories[spec['document_uid']]=directory
        frame=prepare_recovery_frame(store,packet,directories,ops,policy)
        store.advance('recovery-frame:'+paper['paper_id'],None,store.put(frame).sha256)
    if current is not None:frame=store.load(current.source_frame,SourceFrame)
    # Source preparation has its own clock, matching the ordinary competition
    # pipeline. It does not consume the approved post-preparation judging window.
    ops=operations_for_phase('scientific_verification')
    phase('scientific_verification',budget=budget.summary())
    ref=reference(store,current_head) if current_head else None
    for _ in range(3):
        authorization=None;revision=0 if current is None else current.binding.revision
        if current is not None:
            proposal=store.head('source-correction-proposal:'+sha(canonical(current.binding)))
            if proposal is not None and len(current.corrections)<2:
                proposal=reference(store,proposal)
                frame=corrected_frame(store,packet,frame,proposal)
                authorization={'predecessor':ref,'correction':correction_record(store,current,ref,proposal)}
                revision+=1
            elif not any(e.retryable for e in current.hold_reasons):break
        receipt,ref=PaperEvaluator(ops).evaluate(source_packet=packet,source_frame=frame,packages=packages,
            comparison_plan=plan,secret=secret,commitment=commitment,policy_sha256=policy,
            document_bindings={},revision=revision,generation_authorization=authorization)
        current=receipt
        if receipt.state!='HOLD' or getattr(ops,'pending_source_correction',None) is None:break
    phase('finished',budget=budget.summary())
    return {'paper_id':paper['paper_id'],'status':current.state,'receipt_sha256':ref.sha256,'budget':budget.summary()}


def main():
    import argparse
    import os
    from concurrent.futures import ThreadPoolExecutor, as_completed

    from competition.intake_lock import CompetitionBackend

    from .billing import reconcile_usage
    from .contracts import FrozenSettings
    from .errors import safe_message
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--backend-url',required=True)
    parser.add_argument('--round-id',required=True)
    parser.add_argument('--run-id',required=True)
    parser.add_argument('--root',type=Path,required=True)
    parser.add_argument('--paper-id',action='append')
    parser.add_argument('--max-workers',type=int,default=2)
    parser.add_argument('--offline',action='store_true')
    args=parser.parse_args()
    if not 1<=args.max_workers<=10:parser.error('Paper concurrency must be between 1 and 10')
    backend=CompetitionBackend(base_url=args.backend_url,controller_token=os.environ['CLAIMS_COMPETITION_CONTROLLER_TOKEN'])
    manifest=evidence_request(backend,'GET','/internal/competition/scientific/recoveries/'+args.round_id+'/'+args.run_id)
    if manifest['round_id']!=args.round_id or manifest['run_id']!=args.run_id:raise ValueError('Recovery identity differs')
    selected=[p for p in manifest['papers'] if args.paper_id is None or p['paper_id'] in args.paper_id]
    if args.paper_id and {p['paper_id'] for p in selected}!=set(args.paper_id):raise ValueError('Requested paper is outside the frozen recovery sample')
    def worker(paper):
        store=RecoveryStore(args.root,backend,manifest)
        settings=store.load(FileRef.model_validate(manifest['configuration']['scientific_scoring']['settings'],strict=True),FrozenSettings)
        try:
            result=evaluate_paper(store=store,manifest=manifest,paper=paper,settings=settings,offline=args.offline)
            head=store.head('paper:'+json.dumps([args.round_id,paper['stage'],paper['paper_id']]))
            if head and not args.offline:
                receipt=store.load(reference(store,head),SampledSilverReceipt)
                reconciled=[]
                for ref in receipt.judge_runs:
                    from .contracts import JudgeRunReceipt
                    run=store.load(ref,JudgeRunReceipt)
                    key=os.environ.get('CLAIMS_SCIENTIFIC_'+run.role+'_API_KEY') or os.environ.get('OPENROUTER_API_KEY')
                    try:reconciled.append(reconcile_usage(store,run.run_id,key)['status'])
                    except Exception:reconciled.append('unresolved')
                result['billing_reconciliations']={k:reconciled.count(k) for k in set(reconciled)}
            return result
        except Exception as exc:
            keys=[os.environ.get('OPENROUTER_API_KEY'),*[os.environ.get('CLAIMS_SCIENTIFIC_'+r+'_API_KEY') for r in ('SOURCE_READER','PRIMARY','SECONDARY','ARBITER')]]
            message=safe_message(exc,keys)[:2000]
            key='progress:'+paper['stage']+':'+paper['paper_id'];head=store.head(key)
            store.advance(key,head,store.put({'phase':'blocked','updated_at':time.time(),'reason':message}).sha256)
            return {'paper_id':paper['paper_id'],'status':'blocked','reason':message}
    with ThreadPoolExecutor(max_workers=args.max_workers) as executor:
        for future in as_completed([executor.submit(worker,p) for p in selected]):
            print(json.dumps(future.result()),flush=True)


if __name__=='__main__':main()
