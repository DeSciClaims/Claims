"""Process-local native provider auditing for every Hermes/compaction model call."""
import json
import math
import threading
import time


class ProviderAudit:
    def __init__(self,root,*,model,context_window,max_tokens,api_key,reasoning_effort=None,diagnostic_policy=None):
        import tiktoken
        self.root=root;self.model=model;self.context_window=context_window;self.max_tokens=max_tokens
        self.api_key=api_key;self.reasoning_effort=reasoning_effort;self.encoding=tiktoken.get_encoding('cl100k_base')
        self.lock=threading.Lock();self.events=[];self.diagnostic_policy=diagnostic_policy;self.reservations={}
        if diagnostic_policy is not None:
            from .proprietary_diagnostic import validate,Budget
            validate(diagnostic_policy,model);self.budget=Budget(diagnostic_policy['budget_database'])

    def verify_client(self,resource):
        client=resource._client
        if str(client.base_url).rstrip('/')!='https://openrouter.ai/api/v1' or client.api_key!=self.api_key:
            raise ValueError('Native Hermes call uses an unfrozen provider endpoint or credential')
        client.max_retries=0  # Every Hermes retry must pass through this native audit.

    def prepare(self,kwargs):
        kwargs=dict(kwargs)
        if kwargs['model']!=self.model:raise ValueError('Hermes attempted an unfrozen model, including auxiliary calls')
        if kwargs.get('stream'):raise ValueError('Scientific file harness requires auditable non-streaming calls')
        kwargs['max_tokens']=self.max_tokens;kwargs['temperature']=0;kwargs['top_p']=1;kwargs['n']=1
        if kwargs.get('tools'):kwargs['tool_choice']='auto'
        body=dict(kwargs.get('extra_body') or {})
        provider=body.get('provider',{})
        if set(provider)-{'require_parameters','allow_fallbacks'}:raise ValueError('Provider pinning is disabled')
        body['provider']={'require_parameters':True,'allow_fallbacks':True}
        if self.reasoning_effort is not None:body['reasoning']={'effort':self.reasoning_effort,'exclude':True}
        kwargs['extra_body']=body
        # These are the actual decoded messages and tool schemas, not the file
        # corpus. No application source/packet size cap applies to file contents.
        def count(s):return len(self.encoding.encode(s,disallowed_special=()))
        total=32*len(kwargs['messages'])
        for message in kwargs['messages']:
            total+=count(message['role'])
            content=message.get('content')
            if isinstance(content,str):total+=count(content)
            elif isinstance(content,list):
                total+=sum(count(p['text']) for p in content if p.get('type')=='text')
            total+=count(json.dumps({k:v for k,v in message.items() if k not in {'role','content'}},
                                   ensure_ascii=False,separators=(',',':')))
        total+=count(json.dumps(kwargs.get('tools',[]),ensure_ascii=False,separators=(',',':')))
        if total+self.max_tokens>self.context_window:raise ValueError('Actual Hermes request exceeds frozen model context capacity')
        if self.diagnostic_policy:
            kwargs.pop('max_completion_tokens',None)
            for name in self.diagnostic_policy['omitted_parameters']:kwargs.pop(name,None)
            kwargs['extra_body']['reasoning']={'effort':self.diagnostic_policy['reasoning_effort'],'exclude':True}
            token=self.budget.reserve(self.model,total,self.max_tokens)
            self.reservations[id(kwargs)]=token
        return kwargs

    def record(self,kwargs,started,response=None,error=None):
        raw=response.model_dump(mode='json') if response is not None else {}
        usage=raw.get('usage') or {};cost=usage.get('cost')
        if isinstance(cost,dict):cost=cost.get('total_cost')
        if type(cost) not in (int,float) or not math.isfinite(cost) or cost<0:cost=None
        event={'model':kwargs['model'],'api_base':'https://openrouter.ai/api/v1','messages':kwargs['messages'],'tools':kwargs.get('tools',[]),
               'max_tokens':kwargs['max_tokens'],'temperature':kwargs.get('temperature'),'tool_choice':kwargs.get('tool_choice'),
               'provider':kwargs['extra_body']['provider'],'response':raw,'native_usage':usage,
               'actual_cost_usd':cost,'elapsed_seconds':time.monotonic()-started,
               'error':str(error).replace(self.api_key,'[REDACTED]')[:1500] if error else None}
        if self.diagnostic_policy:
            token=self.reservations.pop(id(kwargs));self.budget.settle(token,cost,raw.get('id'))
        with self.lock:
            self.events.append(event)
            with (self.root/'provider_calls.jsonl').open('a') as f:f.write(json.dumps(event,ensure_ascii=False)+'\n')

    def summary(self):
        complete=all(e['actual_cost_usd'] is not None for e in self.events)
        return {'provider_calls':len(self.events),'prompt_tokens':sum(e['native_usage'].get('prompt_tokens',0) for e in self.events),
                'completion_tokens':sum(e['native_usage'].get('completion_tokens',0) for e in self.events),
                'cost':sum(e['actual_cost_usd'] for e in self.events) if complete else None,
                'known_cost':sum(e['actual_cost_usd'] or 0 for e in self.events),
                'unknown_billing_calls':sum(e['actual_cost_usd'] is None for e in self.events)}

    def install(self):
        from openai.resources.chat.completions import Completions,AsyncCompletions
        original=Completions.create;original_async=AsyncCompletions.create;audit=self
        def sync(client,*args,**kwargs):
            audit.verify_client(client)
            def invoke():
                prepared=audit.prepare(kwargs);started=time.monotonic()
                try:result=original(client,*args,**prepared)
                except BaseException as exc:
                    audit.record(prepared,started,error=exc);raise
                audit.record(prepared,started,response=result);return result
            from .diagnostic_dispatch import Dispatch,enabled
            return Dispatch(audit.diagnostic_policy,audit.root).run(invoke) if enabled(audit.diagnostic_policy) else invoke()
        async def asynchronous(client,*args,**kwargs):
            audit.verify_client(client)
            async def invoke():
                prepared=audit.prepare(kwargs);started=time.monotonic()
                try:result=await original_async(client,*args,**prepared)
                except BaseException as exc:
                    audit.record(prepared,started,error=exc);raise
                audit.record(prepared,started,response=result);return result
            from .diagnostic_dispatch import Dispatch,enabled
            return await Dispatch(audit.diagnostic_policy,audit.root).arun(invoke) if enabled(audit.diagnostic_policy) else await invoke()
        Completions.create=sync;AsyncCompletions.create=asynchronous
