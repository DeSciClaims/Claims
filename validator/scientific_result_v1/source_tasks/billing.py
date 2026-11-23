"""Provider-confirmed billing recovery and durable conservative spend reservations.

Generation metadata contract: https://openrouter.ai/docs/api/api-reference/generations/get-generation
A missing ID, timeout, rate limit or absent price never proves a free generation.
"""
import copy
import json
import math
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import is_dataclass, replace
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .operations import Hold


def reconcile_usage(store,run_id,api_key,*,opener=urlopen):
    key='provider-usage:'+run_id;head=store.head(key)
    if head is None:raise KeyError(run_id)
    usage=json.loads(store.get_blob(head))
    if usage['billing_available']:return {'status':'already_observed','run_id':run_id}
    generation=usage.get('provider_request_id')
    if not isinstance(generation,str) or not generation.startswith('gen-'):
        return {'status':'unresolved','run_id':run_id,'reason':'No captured provider generation ID'}
    request=Request('https://openrouter.ai/api/v1/generation?'+urlencode({'id':generation}),headers={'Authorization':'Bearer '+api_key})
    with opener(request,timeout=30) as response:data=json.load(response)['data']
    cost=data.get('total_cost')
    if data.get('id')!=generation or data.get('model')!=usage['model'] or type(cost) not in (int,float) or not math.isfinite(cost) or cost<0:
        raise ValueError('Provider billing identity or cost differs')
    proof=store.put({'kind':'openrouter_generation_billing_v1','previous_usage_sha256':head,'generation':data})
    updated={**usage,'billing_available':True,'usage':{**usage.get('usage',{}),'cost':cost,
        'prompt_tokens':data.get('tokens_prompt'),'completion_tokens':data.get('tokens_completion')},
        'billing_reconciliation':proof.model_dump(mode='json'),'superseded_usage_sha256':head}
    store.advance(key,head,store.put(updated).sha256)
    return {'status':'reconciled','run_id':run_id,'cost_usd':cost}


class BudgetBusy(Hold):
    def __init__(self):
        super().__init__('EVALUATOR','Waiting for in-flight spend reservations',retryable=True)
        self.not_dispatched=True


class RecoveryBudget:
    """Unknown dispatches retain a frozen reservation across crashes; never become zero."""
    def __init__(self,path,*,limit_usd,reserve_usd):
        self.path=Path(path);self.path.parent.mkdir(parents=True,exist_ok=True)
        self.limit=Decimal(str(limit_usd));self.reserve=Decimal(str(reserve_usd))
        if not self.limit.is_finite() or not self.reserve.is_finite() or not 0<self.reserve<=self.limit:
            raise ValueError('Invalid frozen recovery budget')
        with self.connection() as c:
            c.execute('create table if not exists attempts(id integer primary key,model text,stage text,reservation text,cost text,status text)')
            c.execute('create table if not exists policy(id integer primary key,limit_usd text,reserve_usd text)')
            old=c.execute('select limit_usd,reserve_usd from policy where id=1').fetchone()
            if old and old!=(str(self.limit),str(self.reserve)):raise ValueError('Recovery budget cannot change during resume')
            c.execute('insert or ignore into policy values(1,?,?)',(str(self.limit),str(self.reserve)))

    @contextmanager
    def connection(self):
        c=sqlite3.connect(self.path,timeout=30);c.execute('pragma journal_mode=WAL')
        try:
            with c:yield c
        finally:c.close()

    def start(self,model,stage):
        with self.connection() as c:
            c.execute('begin immediate')
            rows=list(c.execute('select reservation,cost,status from attempts'))
            committed=sum((Decimal(cost if cost is not None else reserve) for reserve,cost,_ in rows),Decimal(0))
            if committed+self.reserve>self.limit:
                settled=sum((Decimal(cost if cost is not None else reserve) for reserve,cost,status in rows if status!='in_flight'),Decimal(0))
                if settled+self.reserve<=self.limit and any(status=='in_flight' for _,_,status in rows):raise BudgetBusy()
                exc=Hold('EVALUATOR','Recovery budget exhausted including unresolved dispatch reservations')
                exc.not_dispatched=True
                raise exc
            return c.execute('insert into attempts(model,stage,reservation,status) values(?,?,?,?)',(model,stage,str(self.reserve),'in_flight')).lastrowid

    def finish(self,attempt,usage):
        cost=usage.get('cost')
        observed=type(cost) in (int,float) and math.isfinite(cost) and cost>=0
        with self.connection() as c:
            c.execute('update attempts set cost=?,status=? where id=? and status=?',
                (str(cost) if observed else None,'observed' if observed else 'unknown',attempt,'in_flight'))

    def summary(self):
        with self.connection() as c:rows=list(c.execute('select reservation,cost,status from attempts'))
        return {'limit_usd':float(self.limit),'known_usd':float(sum((Decimal(cost) for _,cost,_ in rows if cost is not None),Decimal(0))),
            'unresolved_reserved_usd':float(sum((Decimal(reserve) for reserve,cost,_ in rows if cost is None),Decimal(0))),
            'unknown_attempts':sum(status=='unknown' for _,_,status in rows),'in_flight_attempts':sum(status=='in_flight' for _,_,status in rows),
            'request_count':len(rows)}


class BudgetedRuntime:
    def __init__(self,runtime,budget):self.runtime=runtime;self.budget=budget;self.admission_deadline=None
    def set_deadline(self,deadline):self.admission_deadline=deadline
    def __getattr__(self,name):return getattr(self.runtime,name)
    def scoped(self):
        runtime=replace(self.runtime,usage=[]) if is_dataclass(self.runtime) else copy.copy(self.runtime)
        if not is_dataclass(self.runtime) and hasattr(runtime,'usage'):runtime.usage=[]
        result=BudgetedRuntime(runtime,self.budget);result.admission_deadline=self.admission_deadline
        return result
    def generate(self,**kwargs):
        while True:
            if self.admission_deadline is not None and time.time()>=self.admission_deadline:
                exc=Hold('EVALUATOR','Deadline expired waiting for in-flight spend reservations',retryable=True)
                exc.not_dispatched=True;raise exc
            try:
                attempt=self.budget.start(getattr(self.runtime,'model','unknown'),kwargs['stage']);break
            except BudgetBusy:
                if self.admission_deadline is None or time.time()>=self.admission_deadline:
                    exc=Hold('EVALUATOR','Deadline expired waiting for in-flight spend reservations',retryable=True)
                    exc.not_dispatched=True;raise exc
                time.sleep(min(.1,max(0,self.admission_deadline-time.time())))
        before=len(getattr(self.runtime,'usage',[]))
        try:return self.runtime.generate(**kwargs)
        finally:
            calls=getattr(self.runtime,'usage',[])
            self.budget.finish(attempt,calls[-1].get('usage',{}) if len(calls)>before else {})
