"""Explicit local-only model compatibility for owner-requested harness experiments.

Competition model allowlists, routes and defaults remain unchanged.
"""
from dataclasses import dataclass
from .store import canonical,sha

MODELS=frozenset({'openai/gpt-5.5','openai/gpt-5.6-sol','openai/gpt-5.4','openai/gpt-5.2',
                 'openai/gpt-6-luna','openai/gpt-5.6-luna','openai/gpt-5.4-mini','openai/gpt-6.1-terra'})


def policy(model, *, context_window_tokens, budget_database,unlimited=False,max_output_tokens=32768,
           max_in_flight=None,reasoning_effort='medium'):
    value={'kind':'claims_local_proprietary_comparison_v1','model':model,
           'context_window_tokens':context_window_tokens,'max_output_tokens':max_output_tokens,
           'reasoning_effort':reasoning_effort,'omitted_parameters':['temperature','top_p','n'],
           'budget_database':str(budget_database),'competition_eligible':False}
    if unlimited:
        value.update(kind="claims_local_proprietary_comparison_v2",request_scheduling="singleflight_retry_after_v1",spend_cap_usd=None)
    if max_in_flight is not None:
        if not unlimited:raise ValueError('Parallel diagnostic profiles require explicit uncapped accounting')
        value.update(kind='claims_local_proprietary_comparison_v3',request_scheduling='bounded_parallel_retry_after_v1',max_in_flight=max_in_flight)
    validate(value,model)
    return value


def validate(value,model):
    parallel=value.get('kind')=='claims_local_proprietary_comparison_v3'
    optional=({'request_scheduling','spend_cap_usd','max_in_flight'} if parallel else
              {'request_scheduling','spend_cap_usd'} if value.get('kind')=='claims_local_proprietary_comparison_v2' else set())
    if (set(value)-optional!={'kind','model','context_window_tokens','max_output_tokens','reasoning_effort',
                     'omitted_parameters','budget_database','competition_eligible'}
        or value['kind'] not in {'claims_local_proprietary_comparison_v1','claims_local_proprietary_comparison_v2','claims_local_proprietary_comparison_v3'} or model not in MODELS
        or value['model']!=model or type(value['max_output_tokens']) is not int
        or value['max_output_tokens'] not in ({32768,65536} if optional else {32768})
        or value['reasoning_effort'] not in ({'low','medium'} if parallel else {'medium'}) or value['competition_eligible'] is not False
        or value['omitted_parameters']!=['temperature','top_p','n']
        or type(value['context_window_tokens']) is not int or value['context_window_tokens']<=value['max_output_tokens']):
        raise ValueError('Invalid local proprietary comparison policy')
    scheduling='bounded_parallel_retry_after_v1' if parallel else 'singleflight_retry_after_v1'
    if optional and (value.get('request_scheduling')!=scheduling or 'spend_cap_usd' not in value or value['spend_cap_usd'] is not None):
        raise ValueError('Invalid unrestricted diagnostic scheduling policy')
    if parallel and (type(value.get('max_in_flight')) is not int or not 1<=value['max_in_flight']<=20):
        raise ValueError('Diagnostic concurrency must be between 1 and 20')
    return value


def parallelism(profiles):
    parallel=[v for v in profiles.values() if v.get('kind')=='claims_local_proprietary_comparison_v3']
    if not parallel:return None
    if len(parallel)!=len(profiles) or len({(v['budget_database'],v['max_in_flight']) for v in parallel})!=1:
        raise ValueError('Parallel judge roles must share one ledger and concurrency ceiling')
    return parallel[0]['max_in_flight']


class Budget:
    """Serialize conservative call reservations across all comparison processes."""
    def __init__(self,path):self.path=str(path)

    def reserve(self,model,prompt_tokens,max_output_tokens):
        import sqlite3,uuid
        with sqlite3.connect(self.path,timeout=30) as c:
            c.execute('begin immediate')
            rate=c.execute('select prompt,completion from rates where model=?',(model,)).fetchone()
            if rate is None:raise ValueError('Model pricing was not frozen')
            maximum=prompt_tokens*rate[0]+max_output_tokens*rate[1]
            spent=c.execute('select coalesce(sum(coalesce(actual,reserved)),0) from calls').fetchone()[0]
            settings=c.execute('select * from settings').fetchone();cap=settings[0]
            active=bool(settings[1]) if len(settings)>1 else True
            if active and spent+maximum>cap:raise ValueError('Comparison spend cap reached; no request dispatched')
            token=uuid.uuid4().hex;c.execute('insert into calls values(?,?,?,?,?,?)',(token,model,maximum,None,'RESERVED',None))
            return token

    def settle(self,token,cost,request_id=None):
        import sqlite3,math
        valid=type(cost) in (int,float) and math.isfinite(cost) and cost>=0
        with sqlite3.connect(self.path,timeout=30) as c:
            c.execute('update calls set actual=?,state=?,request_id=? where token=?',
                      (cost if valid else None,'CONFIRMED' if valid else 'UNKNOWN',request_id,token))

    def summary(self):
        import sqlite3
        with sqlite3.connect(self.path) as c:
            confirmed,conservative,unknown,count=c.execute('select coalesce(sum(actual),0),coalesce(sum(coalesce(actual,reserved)),0),sum(actual is null),count(*) from calls').fetchone()
            return {'confirmed_usd':confirmed,'conservative_committed_usd':conservative,'unknown_billing_calls':unknown or 0,'native_calls':count,'cap_usd':self.cap(c)}


    @staticmethod
    def cap(connection):
        row=connection.execute('select * from settings').fetchone()
        return row[0] if len(row)<2 or row[1] else None

    def remove_cap(self,reason):
        import sqlite3,time
        with sqlite3.connect(self.path,timeout=30) as c:
            c.execute('begin immediate')
            if 'cap_enabled' not in {row[1] for row in c.execute('pragma table_info(settings)')}:
                c.execute('alter table settings add column cap_enabled integer not null default 1')
            old=self.cap(c)
            c.execute('create table if not exists cap_changes(time real,previous_cap real,new_cap real,reason text)')
            c.execute('update settings set cap_enabled=0')
            c.execute('insert into cap_changes values(?,?,?,?)',(time.time(),old,None,reason))


def initialize_budget(path,models,cap):
    import sqlite3
    from pathlib import Path
    Path(path).parent.mkdir(parents=True,exist_ok=True)
    with sqlite3.connect(path) as c:
        c.execute('create table if not exists settings(cap real not null)')
        if not c.execute('select count(*) from settings').fetchone()[0]:c.execute('insert into settings(cap) values(?)',(cap if cap is not None else 0,))
        elif cap is not None and Budget.cap(c)!=cap:raise ValueError('Existing budget cap differs')
        c.execute('create table if not exists rates(model text primary key,prompt real,completion real)')
        c.execute('create table if not exists calls(token text primary key,model text,reserved real,actual real,state text,request_id text)')
        for model in models:
            prices=model['pricing'];overrides=prices.get('overrides',[])
            prompt=max(float(v.get('prompt',prices['prompt'])) for v in [prices,*overrides])
            completion=max(float(v.get('completion',prices['completion'])) for v in [prices,*overrides])
            old=c.execute('select prompt,completion from rates where model=?',(model['id'],)).fetchone()
            if old is not None and old!=(prompt,completion):raise ValueError('Frozen comparison prices changed')
            c.execute('insert or ignore into rates values(?,?,?)',(model['id'],prompt,completion))
    result=Budget(path)
    if cap is None:result.remove_cap("Explicit local comparison without a spending cap")
    return result


def provider_schema(schema):
    """OpenAI-compatible transport; complete original schema remains the validator.

    Nested discriminated oneOf branches are disjoint, so anyOf is equivalent.
    Ordered array constants cannot be expressed in the supported subset: their
    exact bytes/order remain mandatory in the original post-generation contract.
    """
    import copy,json,jsonschema
    def walk(node):
        if isinstance(node,list):return [walk(x) for x in node]
        if not isinstance(node,dict):return node
        branches=node.get('anyOf',[])
        complete=(node.get('type')=='object' and node.get('additionalProperties') is False and branches
            and all(b.get('type')=='object' and b.get('additionalProperties') is False
                and set(b.get('properties',{}))==set(node.get('properties',{}))
                and set(b.get('required',[]))==set(node.get('required',[])) for b in branches))
        # response_schema builds complete outcome-specific object branches. Their
        # common parent is redundant; retaining it exposes optional checks to
        # OpenAI's strict-schema validator. Keep the complete variants unchanged.
        if complete:return {k:walk(v) for k,v in node.items() if k not in {'type','properties','required','additionalProperties','discriminator'}}
        value={k:walk(v) for k,v in node.items() if k!='discriminator'}
        if 'oneOf' in value:
            branches=value.pop('oneOf');keys=[]
            for branch in branches:
                ref=branch.get('$ref')
                target=schema['$defs'][ref.rsplit('/',1)[1]] if ref else branch
                key=target.get('properties',{}).get('kind',{}).get('const')
                if key is None or key in keys:raise ValueError('Cannot losslessly convert an overlapping oneOf')
                keys.append(key)
            value['anyOf']=branches
        if 'prefixItems' in value:
            parts=value.pop('prefixItems')
            if not parts or any(x!=parts[0] for x in parts):raise ValueError('Heterogeneous tuples need an explicit transport')
            value['items']=parts[0]
        if isinstance(value.get('const'),list):
            items=value.pop('const');value.update(minItems=len(items),maxItems=len(items))
            value['description']=value.get('description','')+' Exact ordered controller list remains mandatory; copy it from the request.'
        elif 'const' in value:value['enum']=[value.pop('const')]
        return value
    result=walk(copy.deepcopy(schema));defs=result.pop('$defs',{})
    wire={'type':'object','properties':{'result':result},'required':['result'],'additionalProperties':False,'$defs':defs}
    # Definitions left behind by response-schema specialization need not burden
    # the provider grammar. Remove only definitions unreachable from the root.
    needed=set()
    def references(node):
        if isinstance(node,dict):
            ref=node.get('$ref')
            if isinstance(ref,str) and ref.startswith('#/$defs/'):
                name=ref[len('#/$defs/'):]
                if name not in needed:
                    needed.add(name);references(defs[name])
            for key,value in node.items():
                if key!='$defs':references(value)
        elif isinstance(node,list):
            for value in node:references(value)
    references(wire);wire['$defs']={name:value for name,value in defs.items() if name in needed}
    enumerations=[]
    def collect(node):
        if isinstance(node,dict):
            if isinstance(node.get('enum'),list):enumerations.append(node)
            for value in node.values():collect(value)
        elif isinstance(node,list):
            for value in node:collect(value)
    collect(wire)
    def defer_enum(node):
        values=node.pop('enum')
        if 'type' not in node:
            types={type(x) for x in values}
            if types=={str}:node['type']='string'
            elif types=={int}:node['type']='integer'
            else:raise ValueError('Cannot defer an untyped mixed reference enumeration')
        node['description']=node.get('description','')+' Use an exact original identity from the supplied source/candidate inputs. The complete original allowed pool is enforced after generation.'
    for node in enumerations:
        values=node['enum']
        if len(values)>250 and sum(len(x) for x in values if isinstance(x,str))>15000:defer_enum(node)
    while sum(len(x.get('enum',[])) for x in enumerations)>1000:
        node=max(enumerations,key=lambda x:len(x.get('enum',[])))
        if len(node.get('enum',[]))<=32:raise ValueError('Provider enum budget exceeded by categorical decisions')
        defer_enum(node)
    # A large literal pointer enum already enforces its regex. Keeping both has
    # produced upstream early stops at a pointer prefix despite ample output
    # allowance. Remove only constraints proven redundant for EVERY enum member,
    # after enum deferral, so unconstrained identity pools keep their regex.
    for node in enumerations:
        if 'enum' in node and 'pattern' in node:
            constraint=jsonschema.Draft202012Validator({'pattern':node['pattern']})
            if all(constraint.is_valid(value) for value in node['enum']):node.pop('pattern')
    return wire


def execution_profiles(execution):
    profiles=execution.get('diagnostic_model_profiles',{})
    if not profiles:return {}
    if execution.get('mode')!='local_proprietary_comparison_v1' or set(profiles)!={'SOURCE_READER','PRIMARY','SECONDARY','ARBITER'}:
        raise ValueError('Proprietary profiles require an explicit local-only comparison policy')
    for value in profiles.values():validate(value,value['model'])
    parallelism(profiles)
    return profiles


def compatible_adapter(schema):
    import json,jsonschema
    from .runtime import StrictAdapter
    class Compatible(StrictAdapter):
        def response_format(self):
            return {'type':'json_schema','json_schema':{'name':'claims_source_task','strict':True,'schema':provider_schema(self.schema)}}
        def format(self,*a,**kw):
            messages=super().format(*a,**kw)
            messages[0]['content']+='\nTransport only: wrap the complete original response in a single result property: {"result": ORIGINAL_RESPONSE}. Scientific content and all original constraints are unchanged.'
            return messages
        def parse(self,signature,completion):
            value=json.loads(completion)
            if set(value)!={'result'}:raise ValueError('Expected the provider result envelope')
            try:jsonschema.Draft202012Validator(self.schema).validate(value['result'])
            except jsonschema.ValidationError as error:
                from .response_feedback import schema_feedback
                raise ValueError(schema_feedback(error,self.schema)) from error
            return super().parse(signature,json.dumps(value['result']))
    return Compatible(schema)


def dspy_runtime(*,diagnostic_policy,**kwargs):
    """Same DSPy program/adapter; supported schema envelope and explicit cost guard."""
    from ..runtime import DspyRuntime
    from .runtime import SourceTaskRuntime
    import json,time,tiktoken
    p=diagnostic_policy;validate(p,kwargs['model'])
    class Runtime(SourceTaskRuntime):
        def __post_init__(self):
            if self.max_tokens!=p['max_output_tokens'] or self.temperature is not None or self.transport_retries!=0:
                raise ValueError('Diagnostic runtime differs from its declared policy')
            self._provider_routing()
        def create_adapter(self,schema):return compatible_adapter(schema)
        def _lm(self,**kw):
            lm=super()._lm(**kw)
            for name in p['omitted_parameters']:lm.kwargs.pop(name,None)
            return lm
        def _generate_attempt(self,**kw):
            encoder=tiktoken.get_encoding('cl100k_base');payload=kw['payload'];adapter=self.create_adapter(payload['stage_schema'])
            from types import SimpleNamespace
            messages=adapter.format(SimpleNamespace(__doc__=kw['instructions']),[],{'stage_request':payload})
            tokens=len(encoder.encode(json.dumps({'messages':messages,'response_format':adapter.response_format()},ensure_ascii=False),disallowed_special=()))+1000
            budget=Budget(p['budget_database']);reservation=budget.reserve(self.model,tokens,self.max_tokens);start=len(self.usage)
            try:return DspyRuntime.generate(self,**kw)
            finally:
                completed=[v for v in self.usage[start:] if v.get('event')=='request_complete']
                usage=completed[-1].get('usage',{}) if completed else {}
                cost=usage.get('cost') if completed and completed[-1].get('cost_source')=='provider_usage' else None
                if isinstance(cost,dict):cost=cost.get('total_cost')
                budget.settle(reservation,cost,completed[-1].get('provider_request_id') if completed else None)
        def set_deadline(self,deadline):self.diagnostic_deadline=deadline
        def generate(self,**kw):
            from .diagnostic_dispatch import Dispatch,enabled
            if not enabled(p):return self._generate_attempt(**kw)
            return Dispatch(p,kw['output_dir'],getattr(self,'diagnostic_deadline',None)).run(lambda:self._generate_attempt(**kw))
    return Runtime(**kwargs,temperature=None,max_tokens=p['max_output_tokens'],timeout=600,request_deadline=7200,
                   transport_retries=0,reasoning_effort=p['reasoning_effort'])
