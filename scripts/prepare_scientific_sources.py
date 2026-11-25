"""Prepare revealed scientific sources in stage order alongside package execution.

This operator scheduler calls the round's frozen scientific preparation/runtime.
It does not amend rules, draw a new sample, run packages or publish scores.
"""
from __future__ import annotations

import argparse
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
import json
import os
from pathlib import Path
import signal
import threading
from urllib.parse import urlsplit

from competition.execution_policy import execution_policy
from competition.intake_lock import CompetitionBackend
from competition.round_evaluation import round_evaluation
from competition.scientific_factory import create_scientific_policy
from validator.scientific_result_v1.source_tasks.contracts import SourceDocument, SourcePacket
from validator.scientific_result_v1.source_tasks.operations import Hold
from validator.scientific_result_v1.source_tasks.preparation import prepare_paper
from validator.scientific_result_v1.source_tasks.store import canonical, sha


def ordered_jobs(backend, row):
    plan=round_evaluation(row)
    if plan is None:raise ValueError('Revealed scientific stage plan is required')
    order={s.name:i for i,s in enumerate(plan.stages)}
    sample=[p for p in backend.list_sample(row['round_id']) if p['stage']!='rerun']
    if any(p['stage'] not in order for p in sample):
        raise ValueError('Sample stage differs from frozen evaluation plan')
    sample.sort(key=lambda p:(order[p['stage']],p.get('position',0),p['paper_id']))
    groups={}
    for paper in sample:
        stages=groups.setdefault(paper['paper_id'],[])
        if paper['stage'] not in stages:stages.append(paper['stage'])
    return list(groups.items())


def prepare_round(row, backend, fetcher, *, stop=None, completed=None):
    """Keep at most the published preparation worker count in flight.

    Submission order follows prescreen/screen/final. Completed records are reused;
    failures are reported individually and cannot discard other prepared papers.
    A drain stops new submissions and lets in-flight calls persist their receipts.
    """
    if row['status'] not in {'revealed','screening','final'}:
        return {'status':'waiting_for_revealed_sources','prepared':0,'reused':0,'held':0}
    stop=stop or threading.Event()
    pool=create_scientific_policy(row,backend,row['rules_public']['configuration']).evaluate_pool
    store=pool.store;rid=row['round_id'];configuration=row['rules_public']['configuration']
    root=Path(os.environ['CLAIMS_SCIENTIFIC_EVIDENCE_ROOT'])

    def prepare_one(item):
        paper_id,stages=item
        if store.head('prepared-source:'+rid+':'+paper_id) is not None:
            return {'status':'reused','paper_id':paper_id,'stages':stages}
        meta=backend.get_scientific_source(rid,paper_id)
        raw=fetcher.fetch(meta['source_url'],meta['source_sha256'])
        file=store.put_existing_source(rid,paper_id,raw)
        document=SourceDocument(document_uid='source-'+meta['source_sha256'],role='MAIN',title=paper_id,
            version=meta['source_sha256'],url=meta['source_url'] if not urlsplit(meta['source_url']).query else None,
            required=True,acquisition_status='SUPPLIED',availability='AVAILABLE',file=file)
        packet=SourcePacket(protocol_id='claims-source-task-v1',paper_id=paper_id,round_id=rid,
            state='READY',documents=[document],errors=[])
        parsed=root/'parsed'/sha(canonical([rid,paper_id,meta['source_sha256']]))
        parsed.mkdir(parents=True,exist_ok=True)
        ops=pool.operations_for_paper(rid,stages[0],paper_id,row['configuration_sha256'])
        ops.ocr_scale=configuration['scientific_scoring'].get('recovery',{}).get('source_parser',{}).get('ocr_scale',3.0)
        scope=sha(canonical([rid,paper_id,meta['source_sha256'],row['configuration_sha256']]))
        _,ops.deadline=store.generation('source-preparation:'+scope,scope,timeout=7200)
        try:
            prepare_paper(store=store,packet=packet,parsed_directories={document.document_uid:parsed},
                operations=ops,stages=stages,policy_sha256=row['configuration_sha256'])
        except Hold as exc:
            return {'status':'held','paper_id':paper_id,'stages':stages,'category':exc.category}
        return {'status':'prepared','paper_id':paper_id,'stages':stages}

    counts={'prepared':0,'reused':0,'held':0,'errors':0}
    jobs=iter(ordered_jobs(backend,row))
    limit=execution_policy(row).bronze_max_workers
    with ThreadPoolExecutor(max_workers=limit) as executor:
        running={};exhausted=False
        while running or not exhausted and not stop.is_set():
            while not exhausted and not stop.is_set() and len(running)<limit:
                try:item=next(jobs)
                except StopIteration:exhausted=True;break
                running[executor.submit(prepare_one,item)]=item
            if not running:break
            done,_=wait(running,return_when=FIRST_COMPLETED)
            for future in done:
                item=running.pop(future)
                try:result=future.result()
                except Exception as exc:
                    result={'status':'errors','paper_id':item[0],'stages':item[1],'error_type':type(exc).__name__}
                counts[result['status']]+=1
                if completed is not None:completed(result)
    return {'status':'preparation_drained' if stop.is_set() else 'scientific_source_pass_finished',**counts}


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--round-id',required=True)
    p.add_argument('--backend-url',required=True)
    p.add_argument('--poll-seconds',type=int,default=30)
    args=p.parse_args()
    if not 10<=args.poll_seconds<=600:raise ValueError('Invalid polling interval')
    backend=CompetitionBackend(base_url=args.backend_url,controller_token=os.environ['CLAIMS_COMPETITION_CONTROLLER_TOKEN'])
    from competition.hippius import hippius_s3_client
    from competition.pdf_fetch import TrustedPdfFetcher
    client=hippius_s3_client(endpoint=os.environ['CLAIMS_HIPPIUS_ENDPOINT'],
        access_key_id=os.environ['CLAIMS_HIPPIUS_ACCESS_KEY_ID'],secret_access_key=os.environ['CLAIMS_HIPPIUS_SECRET_ACCESS_KEY'])
    fetcher=TrustedPdfFetcher(allowed_https_hosts=tuple(filter(None,os.environ.get('CLAIMS_COMPETITION_PDF_HTTPS_HOSTS','').split(','))),
        allowed_s3_buckets=tuple(filter(None,os.environ.get('CLAIMS_COMPETITION_PDF_S3_BUCKETS','').split(','))),
        s3_client=client,hippius_endpoint=os.environ['CLAIMS_HIPPIUS_ENDPOINT'])
    stop=threading.Event()
    for sig in (signal.SIGTERM,signal.SIGINT):signal.signal(sig,lambda *_:stop.set())
    def report(result):
        if result['status']!='reused':print(json.dumps({'round_id':args.round_id,**result}),flush=True)
    while not stop.is_set():
        try:
            row=backend.get_round(args.round_id)
            if row['network']!='mainnet':raise ValueError('Scheduler scope differs')
            if row['status'] in {'failed','published'}:break
            result=prepare_round(row,backend,fetcher,stop=stop,completed=report)
            print(json.dumps({'round_id':args.round_id,**result}),flush=True)
        except Exception as exc:
            print(json.dumps({'status':'scheduler_error','error_type':type(exc).__name__}),flush=True)
        stop.wait(args.poll_seconds)
    return 0


if __name__=='__main__':raise SystemExit(main())
