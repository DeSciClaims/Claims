"""Hash-attested setup-timeout audit reader for a frozen scientific generator."""
import hashlib,importlib.util,json,sys
from pathlib import Path

def install(*,directory,manifest,round_id,configuration_sha256,review_sha256):
    from competition import code_audit,pilot_controller_factory,scientific_lineage,scientific_scoring
    from validator.scientific_result_v1.source_tasks.contracts import SampledSilverReceipt
    from validator.scientific_result_v1.source_tasks.store import canonical,sha
    directory=Path(directory);files=json.loads(Path(manifest).read_text())
    loaded={}
    for filename,name in [('setup_timeout_review.py','setup_timeout_review'),('setup_code_audit.py','approved_setup_code_audit'),('setup_scientific_scoring.py','approved_setup_scientific_scoring')]:
        path=directory/filename
        if sha(path.read_bytes())!=files[filename]:raise ValueError('Setup audit reader file hash changed')
        spec=importlib.util.spec_from_file_location('competition.'+name,path)
        module=importlib.util.module_from_spec(spec);sys.modules[spec.name]=module;spec.loader.exec_module(module);loaded[filename]=module
    contract=loaded['setup_timeout_review.py']
    proof_path=directory/'verified_scientific_audit.json'
    if sha(proof_path.read_bytes())!=files[proof_path.name]:raise ValueError('Completed native verification checkpoint changed')
    proof=json.loads(proof_path.read_bytes())
    if proof.get('schema_version')!='claims_source_code_audit_v1' or proof.get('round_id')!=round_id or proof.get('stage')!='final':raise ValueError('Foreign scientific verification checkpoint')
    verified={row['silver_receipt_sha256']:row for row in proof['scientific_lineage'] if 'silver_receipt_sha256' in row}
    original_replay=scientific_lineage.replay
    def cached_replay(store,ref,**kw):
        return completed_receipt(store,ref,verified=verified,round_id=round_id,
            configuration_sha256=configuration_sha256,review_sha256=review_sha256,
            proof_sha256=sha(proof_path.read_bytes()),original_replay=original_replay,**kw)
    def audit(rid,*,backend,**kw):
        record=backend.get_round(rid)
        ref=record.get('rules_public',{}).get('policy',{}).get('setup_timeout_review')
        if (rid!=round_id or record['configuration_sha256']!=configuration_sha256
            or ref is None or ref['sha256']!=review_sha256):raise ValueError('Setup audit reader requires exact owner-approved policy')
        # Worker audits inspect executed jobs, while scientific provenance still
        # sees the complete original sample and all common exclusions.
        excluded={v['paper_id']:v['preparation_exclusion_sha256'] for v in proof['scientific_lineage']
            if 'preparation_exclusion_sha256' in v}
        current=backend._request('GET','/internal/competition/scientific/preparation-exclusions/'+rid)
        if {v['paper_id']:v['receipt_sha256'] for v in current if v['stage']=='final'}!=excluded:
            raise ValueError('Completed verification uses different common exclusions')
        class ExecutedView:
            def __getattr__(self,name):return getattr(backend,name)
            def list_sample(self,rid):return [v for v in backend.list_sample(rid)
                if not (v['stage']=='final' and v['paper_id'] in excluded)]
        def verify(rid,stage,**args):
            args['backend']=backend
            return scientific_lineage.verify_current_grid(rid,stage,**args)
        return loaded['setup_code_audit.py'].collect_final_code_evidence(rid,backend=ExecutedView(),
            scientific_lineage_verifier=verify,**kw)
    def copy_diagnostics(**kwargs):
        rid=kwargs['round_id'];record=kwargs['backend'].get_round(rid)
        ref=record.get('rules_public',{}).get('policy',{}).get('setup_timeout_review')
        if rid!=round_id or record['configuration_sha256']!=configuration_sha256 or ref is None or ref['sha256']!=review_sha256:
            raise ValueError('Copy diagnostic cache requires the approved current round')
        excluded={v['paper_id'] for v in proof['scientific_lineage'] if 'preparation_exclusion_sha256' in v}
        kwargs['paper_ids']=[pid for pid in kwargs['paper_ids'] if pid not in excluded]
        return loaded['setup_scientific_scoring.py'].scientific_copy_diagnostics(**kwargs)
    scientific_scoring.scientific_copy_diagnostics=copy_diagnostics
    code_audit.collect_final_code_evidence=audit
    pilot_controller_factory.collect_final_code_evidence=audit
    scientific_lineage.replay=cached_replay
    return {'setup_timeout_review_sha256':review_sha256,'completed_native_verifications_reused':len(verified),
        'live_heads_plans_and_pair_projection_still_verified':True}


def completed_receipt(store,ref,*,verified,round_id,configuration_sha256,review_sha256,proof_sha256,original_replay,**kw):
    from validator.scientific_result_v1.source_tasks.contracts import SampledSilverReceipt
    # Cache only completed native verification of immutable evidence roots.
    # The caller still verifies the live paper head, complete pair projection,
    # current comparison plan and current scientific policy on every call.
    item=verified.get(ref.sha256)
    if item is None:return original_replay(store,ref,**kw)
    receipt=store.load(ref,SampledSilverReceipt)
    if (receipt.binding.round_id!=round_id or receipt.binding.stage!='final'
        or receipt.binding.policy_sha256!=configuration_sha256 or receipt.binding.paper_id!=item['paper_id']
        or receipt.binding.revision!=item['revision']):raise ValueError('Completed scientific verification scope changed')
    if store.head('paper:'+json.dumps([round_id,'final',receipt.binding.paper_id]))!=ref.sha256:
        raise ValueError('Completed scientific verification has a stale paper head')
    raw=store.get_blob(review_sha256)
    if hashlib.sha256(raw).hexdigest()!=review_sha256:raise ValueError('Setup review approval changed')
    value=json.loads(raw)
    if value['verified_scientific_audit']['sha256']!=proof_sha256:raise ValueError('Verification checkpoint lacks owner authorization')
    if 'seed_continuation_sha256' in item:
        from validator.scientific_result_v1.source_tasks.seed_continuation import verify,key
        binding=receipt.binding;scope=(binding.protocol_id,binding.round_id,binding.stage,binding.paper_id)
        if store.head(key(scope,configuration_sha256))!=item['seed_continuation_sha256']:raise ValueError('Approved original seed continuation changed')
        sample=json.loads(store.get_blob(receipt.sampling_manifest.sha256))
        verify(store,scope=scope,source_packet_sha256=binding.source_packet_sha256,source_frame_sha256=binding.source_frame_sha256,
            current_policy_sha256=configuration_sha256,input_pool_sha256=binding.input_pool_sha256,
            secret=bytes.fromhex(sample['revealed_seed_hex']),original_commitment=sample['seed_commitment'])
    return receipt
