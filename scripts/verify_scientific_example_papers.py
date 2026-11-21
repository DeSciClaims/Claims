"""Retrospective source-task verification of the four retained example-paper packages.

The original extraction JSON/CSVs are immutable inputs, not gold. No extraction is
rerun, no supplement is fetched. Evidence is independently parsed from PDFs by Docling.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import tempfile
import time

from validator.scientific_result_v1.source_tasks.configuration import MODELS, freeze_settings
from validator.scientific_result_v1.source_tasks.contracts import (
    ComparisonPlan, FileRef, SourceDocument, SourceFrame, SourceGridManifest,
    SourcePacket, SubmittedPackage, ErrorRecord,
)
from validator.scientific_result_v1.source_tasks.errors import safe_message
from validator.scientific_result_v1.source_tasks.evaluator import PaperEvaluator
from validator.scientific_result_v1.source_tasks.operations import Hold, retained_usage
from validator.scientific_result_v1.source_tasks.packages import Package
from validator.scientific_result_v1.source_tasks.preparation import prepare_paper
from validator.scientific_result_v1.source_tasks.replay import replay
from validator.scientific_result_v1.source_tasks.runner import APPROVED_MODELS, make_operations
from validator.scientific_result_v1.source_tasks.store import Store, canonical, sha

CASES = {
    'Shukla': ('P001', 'Diabetes Care Shukla et al.pdf'),
    'Genther': ('P002', 'JAMA Genther et al.pdf'),
    'Ohtsuki': ('P003', 'Nature Ohtsuki et al.pdf'),
    'Perel': ('P004', 'BMJ Perel et al.pdf'),
}


def collect_inputs(args, name, store):
    paper_id, filename = CASES[name]
    package_dir = args.packages / name
    identity = json.loads((package_dir / 'run_identity.json').read_text())
    config = json.loads((args.packages / 'benchmark_config.json').read_text())
    trusted = args.trusted_sources / name
    manifest = json.loads((trusted / 'manifest.json').read_text())
    if manifest['paper_id'] != paper_id:
        raise ValueError('Trusted source catalog paper identity differs')
    declared = manifest['documents']
    if [d['pdf_sha256'] for d in declared] != identity['sources']:
        raise ValueError('Source bundle differs from the original miner input hashes')
    documents, parsed = [], {}
    for i, descriptor in enumerate(declared):
        pdf = args.pdfs / filename if i == 0 else trusted / descriptor['pdf_file']
        raw = pdf.read_bytes()
        if sha(raw) != descriptor['pdf_sha256']:
            raise ValueError('Original supplied PDF hash differs')
        cache = args.parsed / name / descriptor['source_id']
        metadata = json.loads((cache / 'source_payload.json').read_text())
        if metadata['source_sha256'] != sha(raw):
            raise ValueError('Retained Docling cache belongs to a different PDF')
        ref = store.put_named('sources/' + descriptor['source_id'] + '.pdf', raw, 'application/pdf')
        uid = 'doc-' + ref.sha256
        documents.append(SourceDocument(document_uid=uid, role='MAIN' if i == 0 else 'SUPPLEMENT',
            title=Path(filename).stem if i == 0 else 'Supplied supplement ' + descriptor['source_id'],
            version=ref.sha256, url=None, required=True, acquisition_status='SUPPLIED',
            availability='AVAILABLE', file=ref))
        parsed[uid] = cache
    packet = SourcePacket(protocol_id='claims-source-task-v1', paper_id=paper_id,
        round_id='retrospective-four-papers-20261121', state='READY', documents=documents, errors=[])
    raw = (package_dir / 'extraction.json').read_bytes()
    data = json.loads(raw)
    csv_refs = []
    with tempfile.TemporaryDirectory(prefix='claims-retained-package-') as work:
        root = Path(work)
        (root / 'output.json').write_bytes(raw)
        for table in data['tables']:
            filename = table['csv_file']
            if Path(filename).name != filename or '\x00' in filename:
                raise ValueError('Unsafe original CSV declaration')
            content = (package_dir / filename).read_bytes()
            (root / filename).write_bytes(content)
            csv_refs.append(store.put_named('a/' + filename, content, 'text/csv'))
        gate = Package(root, 'a', paper_id=paper_id).gate
    package = SubmittedPackage(protocol_id=packet.protocol_id, paper_id=paper_id, round_id=packet.round_id,
        output_alias='a', pipeline_digest=sha(canonical(config['pipeline_sources_sha256'])),
        roles=['BASELINE', 'BRONZE'], source_packet_sha256=sha(canonical(packet)),
        execution_receipt=store.put({'mode': 'historical_extraction_import',
            'run_identity': store.put(identity), 'benchmark_configuration': store.put(config),
            'original_json_sha256': sha(raw)}),
        state='COMPLETED', extraction_json=store.put_named('a/output.json', raw, 'application/json'),
        table_csv_files=csv_refs, errors=[])
    return packet, parsed, package, gate


def diagnostic_comparison(store, packet, package, grid):
    """Use an explicitly empty transport control, never a gold or another miner.

    The competition wire contract requires a pair. Scientific Q for the original
    extraction remains source-only; this local diagnostic creates no ranking rows.
    The control consumes no scientific judge calls and is clearly not a real entry.
    """
    actual=package.model_copy(update={'roles':['CHALLENGER']})
    control=SubmittedPackage(protocol_id=packet.protocol_id,paper_id=packet.paper_id,round_id=packet.round_id,
        output_alias='diagnostic-empty-control',pipeline_digest=sha(b'diagnostic-empty-control-v1'),
        roles=['BASELINE','BRONZE'],source_packet_sha256=sha(canonical(packet)),
        execution_receipt=store.put({'mode':'diagnostic_empty_control','not_a_competition_entry':True,'status':'package_failed'}),
        state='PACKAGE_FAILURE',extraction_json=None,table_csv_files=[],errors=[ErrorRecord(category='PIPELINE',
            code='PACKAGE_MISSING',stage='PACKAGE_COLLECTION',message='Intentional empty transport control for the standalone diagnostic; not a real miner submission.',retryable=False,output_refs=[])])
    plan=ComparisonPlan(protocol_id=packet.protocol_id,round_id=packet.round_id,stage='week_one',
        plan_revision=0,source_grid_revision=0,reference_fallback_used=False,baseline_alias=control.output_alias,
        challenger_aliases=[actual.output_alias],reference_alias=control.output_alias,source_grid=store.put(grid),parent_plan_sha256=None,reason='INITIAL')
    return plan,[control,actual]


def run_case(args, name):
    root = args.output_dir / name
    store = Store(root / 'store')
    prep = evaluation = None
    report = {'paper': name, 'state': 'ERROR', 'Q': None,
              'mode': 'retrospective_verification; not a live competition or accuracy qualification'}
    try:
        packet, parsed, package, gate = collect_inputs(args, name, store)
        if args.reparse_pdfs:
            parsed={d.document_uid:root/'docling'/('S'+str(i+1).zfill(3)) for i,d in enumerate(packet.documents)}
        snapshots = {}
        models = {**MODELS, 'SECONDARY': args.secondary_model}
        for model in set(models.values()):
            value = json.loads((args.snapshots / (model.replace('/', '--') + '.json')).read_text())
            snapshots[model] = value.get('data', value)
        settings, settings_ref = freeze_settings(store, route_snapshots=snapshots, models=models,
            execution_policy={'mode': 'retrospective_four_paper_verification',
                'scientific_accuracy_policy': 'unmeasured_without_truth_corpus'})
        policy = sha(canonical({'settings': settings_ref, 'source_scope_policy': 'original_supplied_sources_only'}))
        inputs = {'settings': settings_ref, 'policy_sha256': policy, 'source_packet': store.put(packet),
                  'submitted_package': store.put(package), 'mechanical_gate': store.put(gate)}
        (root / 'inputs.json').write_text(json.dumps(inputs, default=lambda v: v.model_dump(mode='json'), indent=2))
        report.update(source_evidence_policy='docling_pdf_derivatives_v1', independent_pdf_reparse=args.reparse_pdfs, paper_id=packet.paper_id, source_packet_sha256=sha(canonical(packet)),
            source_files=[{'role': d.role, 'sha256': d.file.sha256} for d in packet.documents],
            original_extraction_sha256=package.extraction_json.sha256,
            mechanical_gate=gate.model_dump(mode='json'), judge_models=models, max_output_tokens=16384,
            source_token_budgets=list(settings.source_token_budgets),
            structured_packet_tokens_per_shard=settings.structured_packet_tokens_per_shard)
        if args.check_inputs_only:
            report['state'] = 'INPUTS_VERIFIED'
        else:
            def ops(prepare=False):
                return make_operations(store=store, settings=settings, policy_sha256=policy,
                    trace_dir=root / 'traces', deadline=time.time() + (7200 if prepare else 1800))
            prep = ops(prepare=True)
            print(name + ': independently parsing PDFs with Docling and preparing text/table source evidence', flush=True)
            prepared, _ = prepare_paper(store=store, packet=packet, parsed_directories=parsed,
                operations=prep, stages=['week_one'], policy_sha256=policy)
            frame = store.load(FileRef.model_validate(prepared['source_frame'], strict=True), SourceFrame)
            order = [case[0] for case in CASES.values()]
            grid = SourceGridManifest(protocol_id=packet.protocol_id, round_id=packet.round_id, stage='week_one',
                revision=0, precommitted_order_sha256=sha(canonical(order)), initial_paper_ids=order, replacements=[])
            plan,packages=diagnostic_comparison(store,packet,package,grid)
            report['comparison_control']='Explicit empty transport control; not a miner or gold extraction; no ranking is produced.'
            inputs['comparison_plan']=store.put(plan)
            inputs['compared_input_packages']=[store.put(value) for value in packages]
            (root/'inputs.json').write_text(json.dumps(inputs,default=lambda value:value.model_dump(mode='json'),indent=2))
            seed = prepared['seeds_by_stage']['week_one']
            evaluation = ops()
            print(name + ': evaluating original JSON/CSV package', flush=True)
            receipt, ref = PaperEvaluator(evaluation).evaluate(source_packet=packet, source_frame=frame,
                packages=packages, comparison_plan=plan,
                secret=store.read(FileRef.model_validate(seed['secret'], strict=True)),
                commitment=seed['commitment'], policy_sha256=policy, document_bindings={})
            replay(store, ref)
            vector = next(v for v in receipt.paper_scores if v.output_alias==package.output_alias)
            report.update(state=receipt.state, receipt=ref.model_dump(mode='json'),
                score=vector.model_dump(mode='json'), Q=vector.score.model_dump() if vector.score else None,
                hold_reasons=[r.model_dump(mode='json') for r in receipt.hold_reasons], offline_replay='passed',
                source_inventory={'table_count': len(frame.tables), 'tile_count': len(frame.tiles)},
                task_card_count=len(receipt.task_cards), assertion_packet_count=len(receipt.assertion_packets),
                scientific_verdict_count=len(receipt.scientific_verdicts))
    except Hold as exc:
        report.update(state='HOLD', category=exc.category, reason=str(exc))
    except Exception as exc:
        secrets = [v for k, v in os.environ.items() if k == 'OPENROUTER_API_KEY' or (k.startswith('CLAIMS_SCIENTIFIC_') and k.endswith('_API_KEY'))]
        report.update(error_type=type(exc).__name__, reason=safe_message(exc, secrets)[:4000])
    # Include retained attempts across restarts, counting each provider dispatch once.
    with store.connection() as connection:
        runs = [FileRef(relative_path='blobs/' + h, sha256=h, byte_length=len(b), media_type='application/octet-stream')
                for h, b in connection.execute("SELECT b.sha,b.data FROM heads h JOIN blobs b ON b.sha=h.sha WHERE h.key LIKE 'judge-run:%'")]
    usage = retained_usage(store, runs)
    report.update(usage=usage, recorded_cost_usd=sum(v.get('cost_usd') or 0 for v in usage),
                  unknown_billing_attempts=sum(v.get('cost_usd') is None for v in usage))
    store.put(report)
    (root / 'report.json').write_text(json.dumps(report, indent=2))
    print(json.dumps({'paper': name, 'state': report['state'], 'Q': report['Q'], 'reason': report.get('reason')}), flush=True)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('packages', 'pdfs', 'trusted-sources', 'parsed', 'snapshots', 'output-dir'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--secondary-model', choices=sorted(APPROVED_MODELS), default=MODELS['SECONDARY'])
    parser.add_argument('--env-file', type=Path)
    parser.add_argument('--concurrency', type=int, choices=(1, 2, 3, 4), default=4)
    parser.add_argument('--check-inputs-only', action='store_true')
    parser.add_argument('--reparse-pdfs', action='store_true',help='Run Docling from original supplied PDF bytes into a validator-owned cache')
    args = parser.parse_args()
    if args.env_file:
        from dotenv import load_dotenv
        load_dotenv(args.env_file)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    with ThreadPoolExecutor(max_workers=args.concurrency) as executor:
        reports = list(executor.map(lambda name: run_case(args, name), CASES))
    summary = {'mode': 'retrospective_four_paper_verification', 'papers': reports,
               'all_scored_and_replayed': all(r['state'] == 'FROZEN' and r.get('offline_replay') == 'passed' for r in reports)}
    (args.output_dir / 'report.json').write_text(json.dumps(summary, indent=2))
    if any(r['state'] == 'ERROR' for r in reports):
        raise SystemExit(2)
    if not args.check_inputs_only and not summary['all_scored_and_replayed']:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
