"""Collect original worker packages into one common source-task evaluation."""
from dataclasses import dataclass
from pathlib import Path
import tempfile
from validator.scientific_result_v1.package import materialize
from validator.scientific_result_v1.source_tasks.contracts import SubmittedPackage,ErrorRecord
from validator.scientific_result_v1.source_tasks.store import canonical,sha
from validator.scientific_result_v1.source_tasks.evaluator import PaperEvaluator
from validator.scientific_result_v1.source_tasks.operations import Hold
from .scientific_scoring import adapt_scientific_receipt
from validator.scientific_result_v1.source_tasks.errors import WORKER_CODES


@dataclass
class ScientificPoolService:
    """Trusted callbacks bind source preparation and pre-execution seed commitments.

    prepared_paper returns source_packet, source_frame, comparison_plan, secret,
    commitment, policy_sha256, and alias_by_hotkey. It must reference the common
    source bytes actually delivered to all workers and the precommitted seed.
    """
    store:object
    prepared_paper:object
    operations_for_paper:object

    def __call__(self,*,round_id,configuration_sha256,rules_revision,paper_id,stage,
                 artifacts_by_hotkey,baseline_hotkey,challenger_hotkeys,
                 worker_outcomes_by_hotkey,**unused):
        prepared=self.prepared_paper(round_id,stage,paper_id)
        aliases=prepared['alias_by_hotkey'];plan=prepared['comparison_plan'];packet=prepared['source_packet']
        expected={baseline_hotkey,*challenger_hotkeys}
        if set(artifacts_by_hotkey)!=expected or not expected<=aliases.keys():raise ValueError('Scientific worker pool differs from frozen entrants')
        if prepared['policy_sha256']!=configuration_sha256:raise ValueError('Scientific prepared policy differs from round')
        if aliases[baseline_hotkey]!=plan.baseline_alias or {aliases[k] for k in challenger_hotkeys}!=set(plan.challenger_aliases):raise ValueError('Scientific plan role mapping differs')
        packages=[];bindings={}
        for hotkey in sorted(expected):
            alias=aliases[hotkey];outcome=worker_outcomes_by_hotkey[hotkey]
            roles=['BASELINE'] if hotkey==baseline_hotkey else ['CHALLENGER']
            if alias==plan.reference_alias:roles.append('BRONZE')
            failure=outcome['status']!='completed'
            extraction_json=None;csv_files=[];artifact=outcome.get('artifact')
            metadata={k:v for k,v in outcome.items() if k!='artifact'}
            collection={'worker_receipt':metadata,'original_worker_receipt_sha256':sha(canonical(outcome)),
                        'artifact_metadata':None,'csv_manifest':{},'extraction_json':None}
            if isinstance(artifact,dict) and artifact.get('schema_version')=='claims_scientific_package_v2':
                collection['artifact_metadata']={k:v for k,v in artifact.items() if k not in {'extraction_json','csv_files'}}
                text=artifact.get('extraction_json')
                if isinstance(text,str):
                    extraction_json=self.store.put_named(alias+'/output.json',text.encode('utf-8'),'application/json')
                    collection['extraction_json']=extraction_json
                for name,text in artifact.get('csv_files',{}).items():
                    if not isinstance(name,str) or not isinstance(text,str):continue
                    # Invalid paths are retained as manifest data, never opened on the host.
                    safe=name if Path(name).name==name and name not in {'.','..',''} and '\\' not in name and '\x00' not in name else 'received-'+sha(name.encode())+'.csv'
                    ref=self.store.put_named(alias+'/'+safe,text.encode('utf-8'),'text/csv');csv_files.append(ref)
                    collection['csv_manifest'][name]=ref
            record_data=dict(protocol_id='claims-source-task-v1',paper_id=paper_id,round_id=round_id,
                output_alias=alias,pipeline_digest=prepared['pipeline_digests'][alias],roles=roles,
                source_packet_sha256=sha(canonical(packet)),execution_receipt=self.store.put(collection),
                state='PACKAGE_FAILURE' if failure else 'COMPLETED',
                table_csv_files=[],errors=[ErrorRecord(category='PIPELINE',code=WORKER_CODES[outcome['status']],stage='EXECUTION',message='Terminal worker failure',retryable=False,output_refs=[])] if failure else [])
            if not failure:
                if artifact!=artifacts_by_hotkey[hotkey]:raise ValueError('Scientific collection changed the immutable worker artifact')
                if artifact.get('schema_version')!='claims_scientific_package_v2':raise Hold('EVALUATOR','Original JSON bytes were not retained by worker transport')
                with tempfile.TemporaryDirectory(prefix='claims-source-pool-') as work:
                    directory=Path(work)/'package';payload=materialize(artifact,directory,paper_id=paper_id,max_bytes=210_000_000)
                    extraction_json=self.store.put_named(alias+'/output.json',(directory/'extraction.json').read_bytes(),'application/json')
                    csv_files=[self.store.put_named(alias+'/'+t['csv_file'],(directory/t['csv_file']).read_bytes(),'text/csv') for t in payload['tables']]
            record_data.update(extraction_json=extraction_json,table_csv_files=csv_files)
            packages.append(SubmittedPackage(**record_data))
        # A separate sealed reference helper must remain in the pool even when not scored.
        if plan.reference_alias not in {p.output_alias for p in packages}:
            reference=prepared.get('reference_package')
            if reference is None:raise Hold('EVALUATOR','Sealed reference helper missing from complete pool')
            packages.append(reference)
        import json
        from validator.scientific_result_v1.source_tasks.contracts import SampledSilverReceipt,FileRef
        from validator.scientific_result_v1.source_tasks.operations import retained_usage
        head=self.store.head('paper:'+json.dumps([round_id,stage,paper_id]))
        if head is not None:
            raw=self.store.get_blob(head)
            ref=FileRef(relative_path='blobs/'+head,sha256=head,byte_length=len(raw),media_type='application/octet-stream')
            current=self.store.load(ref,SampledSilverReceipt)
            pool_hash=sha(canonical(sorted((p.model_dump(mode='json') for p in packages),key=lambda p:p['output_alias'])))
            if (current.binding.input_pool_sha256,current.binding.source_packet_sha256,current.binding.policy_sha256)!=(pool_hash,sha(canonical(packet)),configuration_sha256):
                raise ValueError('Scientific recovery changed original packages, source version or policy')
            if current.state=='FROZEN':
                from validator.scientific_result_v1.source_tasks.replay import replay
                from .scientific_lineage import compatible_plan
                replay(self.store,ref)
                compatible_plan(self.store,current,plan,prepared['precommitted_order'])
                return adapt_scientific_receipt(current,ref,store=self.store,hotkey_by_alias={v:k for k,v in aliases.items()},
                    baseline_alias=plan.baseline_alias,challenger_aliases=plan.challenger_aliases,
                    usage=retained_usage(self.store,current.judge_runs))
        ops=self.operations_for_paper(round_id,stage,paper_id,configuration_sha256)
        receipt,ref=PaperEvaluator(ops).evaluate(source_packet=packet,source_frame=prepared['source_frame'],packages=packages,
            comparison_plan=plan,secret=prepared['secret'],commitment=prepared['commitment'],policy_sha256=configuration_sha256,
            document_bindings=bindings)
        return adapt_scientific_receipt(receipt,ref,store=self.store,hotkey_by_alias={v:k for k,v in aliases.items()},
            baseline_alias=plan.baseline_alias,challenger_aliases=plan.challenger_aliases,
            usage=retained_usage(self.store,receipt.judge_runs))
