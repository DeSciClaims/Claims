"""Construct the frozen DSPy routes for local runs and trusted competition callbacks."""
import json
import os
import time
from .contracts import FileRef,ProviderParameters
from .identity import runtime_descriptor
from .operations import Operations
from .runtime import SourceTaskRuntime
from .store import canonical,sha
from .tokenization import BudgetTokenizer

APPROVED_MODELS=frozenset({'deepseek/deepseek-v4-flash-0731','minimax/minimax-m3.1-flash','qwen/qwen3.8-flash','z-ai/glm-5.3-flash','moonshotai/kimi-k2.5'})


def make_operations(*,store,settings,policy_sha256,trace_dir,deadline=None,offline=False):
    for field,value in runtime_descriptor().items():
        if getattr(settings,field)!=value:raise ValueError('Runtime differs from frozen '+field)
    tokenizer=BudgetTokenizer();routes={r.role:r for r in settings.model_routes};runtimes={}
    for role,route in routes.items():
        params=store.load(route.provider_parameters,ProviderParameters)
        descriptor=json.loads(store.get_blob(route.route_sha256))
        snapshot=json.loads(store.read(FileRef.model_validate(descriptor['snapshot'],strict=True)))
        if sha(canonical(snapshot))!=route.runtime_revision or snapshot['id']!=route.model:raise ValueError('Frozen endpoint snapshot differs')
        if route.model not in APPROVED_MODELS or params.api_base!='https://openrouter.ai/api/v1':raise ValueError('Judge route is not approved')
        if (params.tokenizer_id,params.tokenizer_sha256)!=(tokenizer.id,tokenizer.implementation_sha256):raise ValueError('Pinned tokenizer differs')
        if params.declared_context_window_tokens!=route.context_window_tokens:raise ValueError('Frozen context capacity differs')
        providers=sorted({e['provider_name'] for e in snapshot['endpoints'] if 'structured_outputs' in e.get('supported_parameters',[])})
        if not providers:raise ValueError('No frozen strict-schema provider')
        key=os.environ.get('CLAIMS_SCIENTIFIC_'+role+'_API_KEY') or os.environ.get('OPENROUTER_API_KEY')
        if not key and not offline:raise ValueError('Judge API key missing for '+role)
        runtimes[role]=SourceTaskRuntime(model=route.model,api_key=key or '',api_base=params.api_base,max_tokens=route.max_output_tokens,
            reasoning_effort=params.reasoning_effort,provider_preferences={'require_parameters':True,'allow_fallbacks':True})
    return Operations(store=store,routes=routes,runtimes=runtimes,tokenizers={role:tokenizer for role in routes},
        trace_dir=trace_dir,deadline=deadline or time.time()+1800,policy_hash=policy_sha256,offline=offline)
