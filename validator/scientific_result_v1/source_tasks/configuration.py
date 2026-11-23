"""Construct explicit frozen settings from approved route snapshots and execution policy."""
from typing import get_args,get_origin,Annotated,Literal
from .contracts import FrozenSettings,ModelRoute,ProviderParameters
from .budgets import SOURCE_TOKEN_BUDGETS,STRUCTURED_PACKET_TOKENS
from .identity import runtime_descriptor
from .tokenization import BudgetTokenizer
from .store import sha,canonical

MODELS={'SOURCE_READER':'qwen/qwen3.8-flash','PRIMARY':'qwen/qwen3.8-flash',
        'SECONDARY':'deepseek/deepseek-v4-flash-0731','ARBITER':'moonshotai/kimi-k2.5'}


def literal_default(annotation):
    if get_origin(annotation) is Annotated:return literal_default(get_args(annotation)[0])
    if get_origin(annotation) is Literal and len(get_args(annotation))==1:return get_args(annotation)[0]
    if get_origin(annotation) is tuple:return tuple(literal_default(a) for a in get_args(annotation))
    raise ValueError('Required deployment value has no protocol default')


def freeze_settings(store,*,route_snapshots,execution_policy,models=None,provider_allowlists=None,sealed_evaluation_timeout_seconds=1800):
    """No credentials enter the descriptor. Caller signs/freezes the resulting round config."""
    if provider_allowlists:
        raise ValueError('Judge provider allowlists are not supported; use OpenRouter default routing')
    if type(sealed_evaluation_timeout_seconds) is not int or sealed_evaluation_timeout_seconds not in {1800,7200}:
        raise ValueError('A frozen scientific deadline must be 1800 or owner-approved 7200 seconds')
    execution_policy={**execution_policy,'source_evidence_policy':'docling_pdf_derivatives_v1',
        'provider_allowlists':{},'provider_routing':'openrouter_default'}
    selected=dict(MODELS if models is None else models)
    from .runner import APPROVED_MODELS
    if set(selected)!=set(MODELS) or not set(selected.values())<=APPROVED_MODELS:
        raise ValueError("Frozen routes require all four roles and approved models")
    if len({selected[r] for r in ("PRIMARY","SECONDARY","ARBITER")})!=3 or len({selected[r] for r in ("SOURCE_READER","SECONDARY","ARBITER")})!=3:
        raise ValueError("Independent judge model identities required")
    tokenizer=BudgetTokenizer();routes=[]
    for role,model in selected.items():
        original_snapshot=route_snapshots[model]
        snapshot=original_snapshot
        if snapshot['id']!=model:raise ValueError('Route snapshot model identity differs')
        eligible=[e for e in snapshot['endpoints'] if 'structured_outputs' in e.get('supported_parameters',[])]
        if not eligible:raise ValueError('No strict-schema endpoint for '+model)
        context=min(e['context_length'] for e in eligible)
        provider=ProviderParameters(api_base='https://openrouter.ai/api/v1',tokenizer_id=tokenizer.id,
            tokenizer_sha256=tokenizer.implementation_sha256,declared_context_window_tokens=context,
            tools=(),tool_choice='none',reasoning_effort='minimal' if model=='minimax/minimax-m3.1-flash' else 'none')
        parameters=store.put(provider)
        descriptor={'model':model,'provider':'openrouter','runtime_revision':sha(canonical(snapshot)),
            'snapshot':store.put(snapshot),'original_snapshot':store.put(original_snapshot),'provider_parameters':parameters}
        routes.append(ModelRoute(role=role,provider='openrouter',model=model,runtime_revision=descriptor['runtime_revision'],
            route_sha256=store.put(descriptor).sha256,supports_strict_json_schema=True,
            supports_images='image' in snapshot['architecture']['input_modalities'],context_window_tokens=context,
            temperature=0,top_p=1,n=1,max_output_tokens=16384,request_timeout_seconds=120,provider_parameters=parameters))
    values={**runtime_descriptor(),'model_routes':routes,'execution_policy':store.put(execution_policy),
        'sealed_evaluation_timeout_seconds':sealed_evaluation_timeout_seconds,
        'source_token_budgets':SOURCE_TOKEN_BUDGETS,'structured_packet_tokens_per_shard':STRUCTURED_PACKET_TOKENS}
    for name,field in FrozenSettings.model_fields.items():
        if name not in values:values[name]=literal_default(field.annotation)
    settings=FrozenSettings(**values)
    return settings,store.put(settings)
