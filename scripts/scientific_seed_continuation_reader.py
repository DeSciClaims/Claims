"""Attested, narrowly scoped seed-continuation evaluator and replay adapter."""
import hashlib
import importlib.util
import json
import sys
from pathlib import Path

def verify_batches(original,round_id,stage,*,backend,pairs,round_record):
    """Run unchanged independent paper verification with bounded read concurrency."""
    from concurrent.futures import ThreadPoolExecutor
    grouped={}
    for row in pairs:grouped.setdefault(row['paper_id'],[]).append(row)
    ids=sorted(grouped)
    batches=[[row for pid in ids[i:i+8] for row in grouped[pid]] for i in range(0,len(ids),8)]
    def verify(batch):
        return original(round_id,stage,backend=backend,pairs=batch,round_record=round_record)
    if len(batches)<=1:return verify(pairs)
    with ThreadPoolExecutor(max_workers=min(10,len(batches))) as pool:
        checked=list(pool.map(verify,batches))
    return sorted((row for batch in checked for row in batch),key=lambda row:row['paper_id'])

def install(*,directory,manifest,scope,current_policy_sha256):
    from competition import scientific_pool,scientific_lineage
    from validator.scientific_result_v1.source_tasks import replay as native_replay
    from validator.scientific_result_v1.source_tasks.contracts import SampledSilverReceipt
    directory=Path(directory);expected=json.loads(Path(manifest).read_text())
    loaded={}
    for filename,name in [('seed_continuation.py','seed_continuation'),('seed_evaluator.py','approved_seed_evaluator'),('seed_replay.py','approved_seed_replay'),('errors.py','approved_seed_errors')]:
        file=directory/filename
        if hashlib.sha256(file.read_bytes()).hexdigest()!=expected[filename]:raise ValueError('Seed reader file hash mismatch')
        fullname='validator.scientific_result_v1.source_tasks.'+name
        spec=importlib.util.spec_from_file_location(fullname,file)
        module=importlib.util.module_from_spec(spec);sys.modules[fullname]=module;spec.loader.exec_module(module)
        loaded[filename]=module
    contract=loaded['seed_continuation.py'];original_evaluator=scientific_pool.PaperEvaluator
    original_native_replay=native_replay.replay;original_lineage_replay=scientific_lineage.replay
    original_verify=scientific_lineage.verify_current_grid

    def matches(binding):
        return tuple(getattr(binding,k) for k in ('protocol_id','round_id','stage','paper_id'))==tuple(scope) and binding.policy_sha256==current_policy_sha256

    class ScopedEvaluator:
        def __init__(self,ops):self.ops=ops
        def evaluate(self,**kwargs):
            packet=kwargs['source_packet'];plan=kwargs['comparison_plan']
            requested=(packet.protocol_id,packet.round_id,plan.stage,packet.paper_id)
            if requested!=tuple(scope) or kwargs['policy_sha256']!=current_policy_sha256:
                return original_evaluator(self.ops).evaluate(**kwargs)
            # The changed evaluator differs solely in its commitment verifier.
            # That verifier requires the immutable, scoped continuation record.
            return loaded['seed_evaluator.py'].PaperEvaluator(self.ops).evaluate(**kwargs)

    def replay_wrapper(original):
        def replay(store,ref,**kwargs):
            receipt=store.load(ref,SampledSilverReceipt)
            if not matches(receipt.binding):return original(store,ref,**kwargs)
            return loaded['seed_replay.py'].replay(store,ref,error_registry=loaded['errors.py'])
        return replay

    def verify_grid(round_id,stage,*,backend,pairs,round_record=None):
        if (round_id,stage)!=(scope[1],scope[2]):
            return original_verify(round_id,stage,backend=backend,pairs=pairs,round_record=round_record)
        record=backend.get_round(round_id) if round_record is None else round_record
        if record['configuration_sha256']!=current_policy_sha256:raise ValueError('Continuation verification uses a different current policy')
        result=verify_batches(original_verify,round_id,stage,backend=backend,pairs=pairs,round_record=record)
        for item in result:
            if item['paper_id']!=scope[3] or 'silver_receipt_sha256' not in item:continue
            with scientific_lineage.evidence_store(backend) as store:
                digest=store.head(contract.key(scope,current_policy_sha256))
                if digest is None:raise ValueError('Scientific lineage lost its approved seed continuation')
                item['seed_continuation_sha256']=digest
        return result

    scientific_pool.PaperEvaluator=ScopedEvaluator
    native_replay.replay=replay_wrapper(original_native_replay)
    scientific_lineage.replay=replay_wrapper(original_lineage_replay)
    scientific_lineage.verify_current_grid=verify_grid
    return {'scope':list(scope),'current_policy_sha256':current_policy_sha256,
        'original_commitment_preserved':True,'other_papers_use_original_evaluator':True}
