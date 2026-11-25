"""Construct the frozen DSPy routes for local runs and trusted competition callbacks."""
import json
import os
import time
from .contracts import FileRef,ProviderParameters
from .identity import runtime_descriptor
from .operations import Operations
from .runtime import SourceTaskRuntime
from .store import canonical,sha
from .budgets import SOURCE_TOKEN_BUDGETS,STRUCTURED_PACKET_TOKENS
from .tokenization import BudgetTokenizer

APPROVED_MODELS=frozenset({'deepseek/deepseek-v4-flash-0731','minimax/minimax-m3.1-flash','qwen/qwen3.8-flash','z-ai/glm-5.3-flash','moonshotai/kimi-k2.5'})


def make_operations(*,store,settings,policy_sha256,trace_dir,deadline=None,offline=False,request_gate=None,diagnostic_runtime_controls=None):
    if diagnostic_runtime_controls is not None:
        from .local_runtime_controls import validate
        validate(diagnostic_runtime_controls)
    for field,value in runtime_descriptor().items():
        if getattr(settings,field)!=value:
            amendment=(diagnostic_runtime_controls or {}).get('implementation_amendment',{})
            if not (field=='implementation_digest' and amendment.get('from_implementation_digest')==settings.implementation_digest
                    and amendment.get('to_implementation_digest')==value):
                raise ValueError('Runtime differs from frozen '+field)
    if (settings.source_token_budgets!=SOURCE_TOKEN_BUDGETS
        or settings.structured_packet_tokens_per_shard!=STRUCTURED_PACKET_TOKENS):
        raise ValueError('Runtime review budgets differ from frozen settings')
    tokenizer=BudgetTokenizer();routes={r.role:r for r in settings.model_routes};runtimes={}
    from .harness import validate_harness,hermes_identity,TOOLS
    execution=json.loads(store.read(settings.execution_policy))
    tolerance=execution.get('failure_tolerance_diagnostic')
    if diagnostic_runtime_controls is not None and (tolerance is None or execution.get('mode')!='failure_tolerance_diagnostic'):
        raise ValueError('Runtime controls require a local failure-tolerance diagnostic')
    if tolerance is not None and execution.get('mode')!='failure_tolerance_diagnostic':
        raise ValueError('Failure-tolerant scoring is restricted to an explicit local diagnostic')
    harness=validate_harness(execution.get('judge_harness'))
    from .proprietary_diagnostic import execution_profiles,dspy_runtime
    diagnostic_profiles=execution_profiles(execution)
    python=os.environ.get('CLAIMS_SCIENTIFIC_HERMES_PYTHON')
    if harness and not offline and (not python or hermes_identity(python)!=harness['runtime_identity']):
        raise ValueError('Hermes runtime differs from frozen identity')
    for role,route in routes.items():
        params=store.load(route.provider_parameters,ProviderParameters)
        descriptor=json.loads(store.get_blob(route.route_sha256))
        snapshot=json.loads(store.read(FileRef.model_validate(descriptor['snapshot'],strict=True)))
        if sha(canonical(snapshot))!=route.runtime_revision or snapshot['id']!=route.model:raise ValueError('Frozen endpoint snapshot differs')
        diagnostic=diagnostic_profiles.get(role)
        if diagnostic and diagnostic['model']!=route.model:raise ValueError('Diagnostic route identity changed')
        if route.model not in APPROVED_MODELS|({diagnostic['model']} if diagnostic else set()) or params.api_base!='https://openrouter.ai/api/v1':raise ValueError('Judge route is not approved')
        if (params.tokenizer_id,params.tokenizer_sha256)!=(tokenizer.id,tokenizer.implementation_sha256):raise ValueError('Pinned tokenizer differs')
        if params.declared_context_window_tokens!=route.context_window_tokens:raise ValueError('Frozen context capacity differs')
        providers=sorted({e['provider_name'] for e in snapshot['endpoints'] if 'structured_outputs' in e.get('supported_parameters',[])})
        if not providers:raise ValueError('No frozen strict-schema provider')
        expected=(TOOLS,'required' if harness['version']==5 else 'auto',32768,harness['stage_timeout_seconds']) if harness else ((),'none',diagnostic['max_output_tokens'],7200) if diagnostic else ((),'none',16384,120)
        if (params.tools,params.tool_choice,route.max_output_tokens,route.request_timeout_seconds)!=expected:
            raise ValueError('Provider parameters differ from frozen harness')
        if (route.temperature,route.top_p,route.n)!=((None,None,None) if diagnostic else (0,1,1)):
            raise ValueError('Sampling parameters differ from explicit route policy')
        key=os.environ.get('CLAIMS_SCIENTIFIC_'+role+'_API_KEY') or os.environ.get('OPENROUTER_API_KEY')
        if not key and not offline:raise ValueError('Judge API key missing for '+role)
        if harness:
            from .file_runtime import FileAgentRuntime
            runtimes[role]=FileAgentRuntime(store=store,route=route,harness=harness,python=python,
                api_key=key or '',reasoning_effort=params.reasoning_effort,diagnostic_policy=diagnostic)
        elif diagnostic:
            runtimes[role]=dspy_runtime(diagnostic_policy=diagnostic,model=route.model,api_key=key or '',
                api_base=params.api_base,provider_preferences={'require_parameters':True,'allow_fallbacks':True})
        else:
            runtimes[role]=SourceTaskRuntime(model=route.model,api_key=key or '',api_base=params.api_base,max_tokens=route.max_output_tokens,
                reasoning_effort=params.reasoning_effort,provider_preferences={'require_parameters':True,'allow_fallbacks':True})
    return Operations(store=store,routes=routes,runtimes=runtimes,tokenizers={role:tokenizer for role in routes},
        trace_dir=trace_dir,deadline=deadline or time.time()+settings.sealed_evaluation_timeout_seconds,policy_hash=policy_sha256,offline=offline,
        generation_timeout_seconds=settings.sealed_evaluation_timeout_seconds,harness=harness,diagnostic_profiles=diagnostic_profiles,
        failure_tolerance_policy=tolerance,gate=request_gate,diagnostic_runtime_controls=diagnostic_runtime_controls)
