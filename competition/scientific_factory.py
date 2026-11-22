"""Built-in scientific round binding; private prepared sources never enter claim parsers."""
import json
import os
from pathlib import Path
from validator.scientific_result_v1.source_tasks.contracts import (FrozenSettings,ProviderParameters,SourcePacket,SourceFrame,
    ComparisonPlan,FileRef,SubmittedPackage)
from validator.scientific_result_v1.source_tasks.store import sha,canonical
from validator.scientific_result_v1.source_tasks.remote import RemoteStore
from validator.scientific_result_v1.source_tasks.runtime import SourceTaskRuntime
from validator.scientific_result_v1.source_tasks.operations import Operations
from validator.scientific_result_v1.source_tasks.tokenization import BudgetTokenizer
from .scientific_policy import FrozenScientificEvaluationPolicy
from .scientific_pool import ScientificPoolService
from .scientific_validation import TrustedScientificDeliveryValidator

APPROVED_MODELS=frozenset({'deepseek/deepseek-v4-flash-0731','minimax/minimax-m3.1-flash','qwen/qwen3.8-flash','z-ai/glm-5.3-flash','moonshotai/kimi-k2.5'})


def _root():
    path=os.environ.get('CLAIMS_SCIENTIFIC_EVIDENCE_ROOT')
    if not path:raise ValueError('CLAIMS_SCIENTIFIC_EVIDENCE_ROOT is required')
    return Path(path)


def _source(store,round_id,paper_id):
    head=store.head('prepared-source:'+round_id+':'+paper_id)
    if head is None:raise ValueError('Source inventory and paper-secret commitments must be prepared before execution')
    value=json.loads(store.get_blob(head))
    value['source_packet']=store.load(FileRef.model_validate(value['source_packet'],strict=True),SourcePacket)
    value['source_frame']=store.load(FileRef.model_validate(value['source_frame'],strict=True),SourceFrame)
    return value


def create_delivery_validator(backend,spec):
    store=RemoteStore(_root(),backend)
    def source(job):
        ensure_stage_plan(store,backend,job['round_id'],job['stage'])
        prepared=_source(store,job['round_id'],job['paper_id'])
        return prepared['source_packet']
    return TrustedScientificDeliveryValidator(source,_root()/'delivery-reports',store.read)


def create_scientific_policy(row,backend,configuration):
    store=RemoteStore(_root(),backend);spec=configuration['scientific_scoring']
    settings=store.load(FileRef.model_validate(spec['settings'],strict=True),FrozenSettings)
    if spec.get('settings_sha256')!=sha(canonical(settings)):raise ValueError('Scientific settings binding differs')
    from validator.scientific_result_v1.source_tasks.identity import runtime_descriptor
    for field,expected in runtime_descriptor().items():
        if getattr(settings,field)!=expected:raise ValueError('Scientific runtime differs from frozen '+field)
    public_policy=row.get('rules_public',{}).get('policy',{})
    if public_policy.get('validation_protocol')!='claims-source-task-v1' or public_policy.get('winner_selection')!='silver_tiebreak':
        raise ValueError('Scientific round rules must freeze protocol dispatch and Silver-only selection')
    from .code_audit import SOURCE_AUDIT_POLICY
    if public_policy.get('code_audit')!=SOURCE_AUDIT_POLICY:raise ValueError('Scientific round must freeze the three-check code audit policy')
    mode=spec.get('mode')
    if mode not in {'testnet_smoke','production'} or (mode=='testnet_smoke' and row.get('network')!='testnet'):
        raise ValueError('Scientific activation mode is invalid')
    if mode=='production':
        from validator.scientific_result_v1.source_tasks.qualification import activation_failures
        if not isinstance(spec.get('activation_receipt'),dict):raise ValueError('Production requires signed scientific qualification evidence')
        activation=json.loads(store.read(FileRef.model_validate(spec['activation_receipt'],strict=True)))
        from substrateinterface import Keypair
        signer=spec['qualification_signer'];signed=activation['report']
        if not Keypair(ss58_address=signer).verify(canonical(signed),bytes.fromhex(activation['signature_hex'])):raise ValueError('Scientific qualification signature invalid')
        if spec.get('scientific_accuracy_policy')!='unmeasured_without_truth_corpus':
            raise ValueError('Scientific policy must disclose the waived truth-corpus requirement and unmeasured accuracy')
        failures=activation_failures(signed,truth_corpus_required=False)
        if failures:raise ValueError('Scientific production qualification incomplete: '+', '.join(failures))
    tokenizer=BudgetTokenizer();routes={r.role:r for r in settings.model_routes}
    params={role:store.load(route.provider_parameters,ProviderParameters) for role,route in routes.items()}
    preferences={}
    for role,route in routes.items():
        descriptor=json.loads(store.get_blob(route.route_sha256))
        snapshot=json.loads(store.read(FileRef.model_validate(descriptor['snapshot'],strict=True)))
        if sha(canonical(snapshot))!=route.runtime_revision or snapshot['id']!=route.model:
            raise ValueError('Frozen route endpoint snapshot differs')
        providers=sorted({e['provider_name'] for e in snapshot['endpoints'] if 'structured_outputs' in e.get('supported_parameters',[])})
        if not providers:raise ValueError('No strict-schema provider advertised for judge model')
        preferences[role]={'require_parameters':True,'allow_fallbacks':True}
    for role,route in routes.items():
        p=params[role]
        if p.declared_context_window_tokens!=route.context_window_tokens:raise ValueError('Scientific route context descriptor differs')
        if route.model not in APPROVED_MODELS or p.api_base!='https://openrouter.ai/api/v1':raise ValueError('Scientific route is not approved')
        if (p.tokenizer_id,p.tokenizer_sha256)!=(tokenizer.id,tokenizer.implementation_sha256):raise ValueError('Scientific tokenizer identity differs')
    def prepared(round_id,stage,paper_id):
        source=_source(store,round_id,paper_id)
        ensure_stage_plan(store,backend,round_id,stage)
        head=store.head('scientific-plan:'+round_id+':'+stage)
        if head is None:raise ValueError('Frozen scientific comparison plan missing')
        plan_data=json.loads(store.get_blob(head))
        plan=store.load(FileRef.model_validate(plan_data['comparison_plan'],strict=True),ComparisonPlan)
        seed=source['seeds_by_stage'][stage]
        source.update(comparison_plan=plan,secret=store.read(FileRef.model_validate(seed['secret'],strict=True)),
            commitment=seed['commitment'],policy_sha256=row['configuration_sha256'],alias_by_hotkey=plan_data['alias_by_hotkey'],
            pipeline_digests=plan_data['pipeline_digests'],document_uid_by_source_ordinal=source['document_uid_by_source_ordinal'],precommitted_order=plan_data['precommitted_order'])
        if plan_data.get('reference_package') is not None:source['reference_package']=store.load(FileRef.model_validate(plan_data['reference_package'],strict=True),SubmittedPackage)
        return source
    def operations(round_id,stage,paper_id,policy_hash):
        import time
        runtimes={}
        for role,route in routes.items():
            key=os.environ.get('CLAIMS_SCIENTIFIC_'+role+'_API_KEY') or os.environ.get('OPENROUTER_API_KEY')
            if not key:raise ValueError('Scientific judge API key missing for '+role)
            runtimes[role]=SourceTaskRuntime(model=route.model,api_key=key,api_base=params[role].api_base,max_tokens=route.max_output_tokens,reasoning_effort=params[role].reasoning_effort,provider_preferences=preferences[role])
        return Operations(store=store,routes=routes,runtimes=runtimes,tokenizers={r:tokenizer for r in routes},
            trace_dir=_root()/'traces'/sha(canonical([round_id,stage,paper_id])),deadline=time.time()+settings.sealed_evaluation_timeout_seconds,policy_hash=policy_hash,
            generation_timeout_seconds=settings.sealed_evaluation_timeout_seconds)
    pool=ScientificPoolService(store,prepared,operations)
    return FrozenScientificEvaluationPolicy(row['round_id'],row['configuration_sha256'],configuration,pool,int(row.get('rules_revision',1)))


class PreparedScientificPaperInputs:
    def __init__(self,base,store):self.base=base;self.store=store
    def __getattr__(self,name):return getattr(self.base,name)
    def _main(self,stage,paper_id):
        self.base._load_sample()
        prepared=_source(self.store,self.base.round_id,paper_id)
        packet=prepared['source_packet']
        main=next(d for d in packet.documents if d.role=='MAIN')
        expected=self.base._sample.get((stage,paper_id))
        if expected is None or main.file.sha256!=expected:raise ValueError('Scientific source differs from released paper version')
        return main
    def pdf_source_for_paper(self,stage,paper_id):
        main=self._main(stage,paper_id)
        return 'scientific-blob:'+main.file.sha256,main.file.sha256
    def paper_metadata_for_paper(self,stage,paper_id):
        main=self._main(stage,paper_id)
        return {'paper_id':paper_id,'title':main.title}


class PreparedScientificPdfFetcher:
    def __init__(self,store):self.store=store
    def fetch(self,url,expected_sha256):
        if url!='scientific-blob:'+expected_sha256:raise ValueError('Scientific source URL/hash mismatch')
        return self.store.get_blob(expected_sha256)


def bind_scientific_worker_sources(base,backend):
    store=RemoteStore(_root(),backend)
    return PreparedScientificPaperInputs(base,store),PreparedScientificPdfFetcher(store)


def create_source_preparer(row,backend,configuration,fetcher):
    """The former Bronze preparation slot prepares source tasks, never claim-unit truth."""
    import time
    from .scientific_factory import create_scientific_policy
    from validator.scientific_result_v1.source_tasks.contracts import SourceDocument
    from validator.scientific_result_v1.source_tasks.preparation import prepare_paper
    from validator.scientific_result_v1.source_tasks.operations import Hold
    from miner.scientific_result_v1.docling_source import parse_pdf
    policy=create_scientific_policy(row,backend,configuration);pool=policy.evaluate_pool
    def prepare(round_id):
        if round_id!=row['round_id']:raise ValueError('Scientific preparation round mismatch')
        sample=backend.list_sample(round_id);groups={}
        for paper in sample:
            if paper['stage']=='rerun':continue
            groups.setdefault(paper['paper_id'],[]).append(paper['stage'])
        prepared=[];holds=[]
        for paper_id,stages in groups.items():
            if pool.store.head('prepared-source:'+round_id+':'+paper_id) is not None:continue
            meta=backend.get_scientific_source(round_id,paper_id)
            raw=fetcher.fetch(meta['source_url'],meta['source_sha256'])
            file=pool.store.put_existing_source(round_id,paper_id,raw)
            document=SourceDocument(document_uid='source-'+meta['source_sha256'],role='MAIN',title=paper_id,version=meta['source_sha256'],url=meta['source_url'] if not __import__('urllib.parse',fromlist=['urlsplit']).urlsplit(meta['source_url']).query else None,required=True,acquisition_status='SUPPLIED',availability='AVAILABLE',file=file)
            packet=SourcePacket(protocol_id='claims-source-task-v1',paper_id=paper_id,round_id=round_id,state='READY',documents=[document],errors=[])
            parsed=_root()/'parsed'/meta['source_sha256'];parsed.mkdir(parents=True,exist_ok=True)
            pdf=parsed/'input.pdf'
            if not pdf.exists():pdf.write_bytes(raw)
            if not (parsed/'source_payload.json').exists():parse_pdf(pdf,parsed)
            ops=pool.operations_for_paper(round_id,stages[0],paper_id,row['configuration_sha256'])
            # Source preparation is measured separately from the immutable post-seal deadline.
            ops.deadline=time.time()+7200
            try:
                prepare_paper(store=pool.store,packet=packet,parsed_directories={document.document_uid:parsed},operations=ops,stages=stages,policy_sha256=row['configuration_sha256'])
                prepared.append(paper_id)
            except Hold as exc:holds.append({'paper_id':paper_id,'category':exc.category,'reason':str(exc)})
        return {'status':'scientific_source_hold' if holds else 'scientific_sources_prepared','prepared_papers':prepared,'holds':holds}
    return prepare


def ensure_stage_plan(store,backend,round_id,stage):
    from validator.scientific_result_v1.source_tasks.contracts import SourceGridManifest
    key='scientific-plan:'+round_id+':'+stage
    if store.head(key) is not None:return
    record=backend.get_round(round_id)
    ledger=next((s for s in backend.list_stages(round_id) if s['stage']==stage),None)
    if ledger is None:raise ValueError('Scientific stage entrants are not frozen')
    entries=ledger['entries'];baselines=[e for e in entries if e['role'] in {'reference','incumbent'}]
    if len(baselines)!=1:raise ValueError('Scientific stage needs exactly one baseline')
    sample=[p for p in backend.list_sample(round_id) if p['stage']==stage]
    pool=backend.list_paper_pool(round_id,reference_release_id=record['reference_release_id'])
    order=[p['paper_id'] for p in sorted(pool,key=lambda p:p['paper_id'].encode())]
    grid=SourceGridManifest(protocol_id='claims-source-task-v1',round_id=round_id,stage=stage,revision=0,
        precommitted_order_sha256=sha(canonical(order)),initial_paper_ids=[p['paper_id'] for p in sample],replacements=[])
    aliases={e['hotkey']:'output-'+sha(canonical([round_id,stage,e['hotkey'],e['package_sha256']])) for e in entries}
    reference=aliases[baselines[0]['hotkey']] if baselines[0]['role']=='reference' else 'reference-'+record['reference_package_sha256']
    plan=ComparisonPlan(protocol_id='claims-source-task-v1',round_id=round_id,stage=stage,plan_revision=0,source_grid_revision=0,
        reference_fallback_used=False,baseline_alias=aliases[baselines[0]['hotkey']],challenger_aliases=[aliases[e['hotkey']] for e in entries if e['role']=='challenger'],
        reference_alias=reference,source_grid=store.put(grid),parent_plan_sha256=None,reason='INITIAL')
    value={'comparison_plan':store.put(plan).model_dump(mode='json'),'alias_by_hotkey':aliases,
        'pipeline_digests':{aliases[e['hotkey']]:e['package_sha256'] for e in entries},'precommitted_order':order}
    ref=store.put(value)
    from validator.scientific_result_v1.source_tasks.store import Conflict
    try:store.advance(key,None,ref.sha256)
    except Conflict:
        current=json.loads(store.get_blob(store.head(key)))
        if current!=value:raise
