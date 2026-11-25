"""Offline candidate-retrieval comparison on saved, hash-verified testnet cases.

Never dispatches a model call or writes the original evidence store. Measures
recovery of earlier matching candidates, not scientific truth or final accuracy.
"""
import argparse
from contextlib import contextmanager
from pathlib import Path
import json
import sqlite3
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from validator.scientific_result_v1.source_tasks.store import Store
from validator.scientific_result_v1.source_tasks.contracts import (FileRef, SampledSilverReceipt,
    SourceFrame, TaskCard, AssertionPacket, SubmittedPackage)
from validator.scientific_result_v1.source_tasks.packets import Index
from validator.scientific_result_v1.source_tasks.packages import Package
from validator.scientific_result_v1.source_tasks.budgeted_validation import PROFILE, match
from validator.scientific_result_v1.source_tasks.evaluator import evidence_for
from validator.scientific_result_v1.source_tasks.budgeted_batches import pooled
from validator.scientific_result_v1.source_tasks.tokenization import BudgetTokenizer
from validator.scientific_result_v1.source_tasks.store import canonical


class ReadOnlyStore(Store):
    def __init__(self, root):
        self.root=Path(root).resolve(); self.db=self.root/'evidence.sqlite3'
        if not self.db.is_file():raise FileNotFoundError(self.db)

    @contextmanager
    def connection(self):
        c=sqlite3.connect(self.db.as_uri()+'?mode=ro',uri=True)
        try:yield c
        finally:c.close()


class Captured(Exception):
    pass


def owner(pointer):
    pieces=pointer.split('/')
    return '/'.join(pieces[:3]) if len(pieces)>=3 and pieces[1] in {'results','shared_context','result_groups','cross_group_summaries'} else pointer


def inspect(evaluation, candidate_components=96):
    report=json.loads((evaluation/'report.json').read_text());store=ReadOnlyStore(evaluation/'store')
    receipt=store.load(FileRef.model_validate(report['receipt'],strict=True),SampledSilverReceipt)
    frame=store.load(receipt.source_frame,SourceFrame)
    tasks={task.task_id:task for ref in receipt.task_cards for task in [store.load(ref,TaskCard)]}
    cases={}
    for ref in receipt.assertion_packets:
        case=store.load(ref,AssertionPacket);task=tasks[case.task_id]
        if task.task_kind=='CONTENT' and '::shard:' not in task.task_id:
            cases.setdefault(case.output_alias,{})[case.task_id]=case
    details=[]; requests=[]; counter=BudgetTokenizer()
    packages=[store.load(ref,SubmittedPackage) for ref in receipt.input_packages]
    for submitted in packages:
        if submitted.output_alias not in cases:continue
        with tempfile.TemporaryDirectory(prefix='claims-offline-retrieval-') as work:
            root=Path(work);(root/'output.json').write_bytes(store.read(submitted.extraction_json))
            for ref in submitted.table_csv_files:(root/Path(ref.relative_path).name).write_bytes(store.read(ref))
            package=Package(root,submitted.output_alias)
            if not package.gate.gate_bit:continue
            index=Index(package)
            for task_id,case in cases[submitted.output_alias].items():
                index.document_uid_by_source={b.source_id:b.document_uid for b in case.document_bindings if b.state=='BOUND'}
                captured=[]
                class Capture:
                    budgeted_profile={**PROFILE,'candidate_components':candidate_components}
                    def request(self,role,operation,data,response_type,**kwargs):
                        captured.append(data);raise Captured()
                task=tasks[task_id]
                try:match(index,task,Capture(),evidence_for(frame,task.evidence_anchor_ids),'PRIMARY')
                except Captured:pass
                data=captured[0];selected=data['inspection_component_ids']
                expected={owner(ref.pointer) for ref in case.match.candidate_refs if ref.kind=='json'}
                found={owner(pointer) for pointer in selected}
                details.append(dict(task_id=task_id,output_alias=submitted.output_alias,
                    available_components=len(index.components),selected_components=len(selected),
                    known_candidate_owners=len(expected),recovered_candidate_owners=len(expected&found),
                    all_known_owners_recovered=expected<=found,
                    shortlist_payload_tokens=counter(canonical(data).decode())))
                requests.append(dict(key=task_id,data=data))
    # Batching shares the original evidence within one miner only.
    batches=[]
    for alias in cases:
        selected=[r for r in requests if r['data']['output_alias']==alias]
        for start in range(0,len(selected),PROFILE['matching_batch_size']):
            batches.append(counter(canonical(pooled(selected[start:start+PROFILE['matching_batch_size']])).decode()))
    count=sum(v['known_candidate_owners'] for v in details)
    recovered=sum(v['recovered_candidate_owners'] for v in details)
    return dict(paper_id=receipt.binding.paper_id,cases=len(details),
        matching_batches=len(batches),batched_payload_tokens=sum(batches),
        known_candidate_owners=count,recovered_candidate_owners=recovered,
        candidate_owner_recovery_fraction=recovered/count if count else None,
        cases_with_all_known_owners_recovered=sum(v['all_known_owners_recovered'] for v in details),
        cases_detail=details)


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--saved-root',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--paper',action='append')
    p.add_argument('--candidate-components',type=int,default=PROFILE['candidate_components'],
                   help='Offline planning only: compare shortlist sizes without changing the live diagnostic profile')
    args=p.parse_args(); rows=[]
    for folder in sorted(args.saved_root.glob('*/evaluation')):
        if args.paper and folder.parent.name not in args.paper:continue
        row=inspect(folder,args.candidate_components);rows.append(row)
        args.output.parent.mkdir(parents=True,exist_ok=True)
        args.output.write_text(json.dumps(dict(kind='offline_retrieval_diagnostic_v1',
            provider_calls=0,profile={**PROFILE,'candidate_components':args.candidate_components},papers=rows,
            interpretation='Earlier matching candidates are a reference, not gold truth. Omission detection and scientific accuracy require live evaluation.'),indent=2)+'\n')
        print(json.dumps({k:v for k,v in row.items() if k!='cases_detail'}),flush=True)


if __name__=='__main__':main()
