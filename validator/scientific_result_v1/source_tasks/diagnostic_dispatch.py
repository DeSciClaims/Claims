"""Local comparison queue and exact OpenRouter in-flight admission retries."""
from contextlib import contextmanager,asynccontextmanager
from pathlib import Path
import asyncio,fcntl,json,time,random
from email.utils import parsedate_to_datetime


def inflight_throttle(error):
    """Only the transient admission-control 402, never generic billing errors."""
    status=getattr(error,'status_code',None)
    candidates=[getattr(error,'body',None)]
    text=str(error);start=text.find('{')
    if start>=0:
        try:candidates.append(json.JSONDecoder().raw_decode(text[start:])[0])
        except ValueError:pass
    meta=None
    for body in candidates:
        if not isinstance(body,dict):continue
        data=body.get('error',body)
        if not isinstance(data,dict):continue
        candidate=data.get('metadata',{})
        if ((status or data.get('code'))==402 and candidate.get('reason')=='in_flight_budget_exhausted'
            and candidate.get('limit_source')=='openrouter_in_flight_budget'):
            meta=candidate;break
    if meta is None:return None
    headers=getattr(getattr(error,'response',None),'headers',{}) or {}
    raw=headers.get('Retry-After') or headers.get('retry-after') or meta.get('headers',{}).get('Retry-After')
    if raw is None:delay=120.0
    else:
        try:delay=max(0,float(raw))
        except (TypeError,ValueError):delay=max(0,parsedate_to_datetime(raw).timestamp()-time.time())
    return {'reason':meta['reason'],'limit_source':meta['limit_source'],'retry_after_seconds':delay}


def enabled(profile):return profile is not None and profile.get('request_scheduling') in {'singleflight_retry_after_v1','bounded_parallel_retry_after_v1'}


def worker_limit(ops,*,nested=False):
    """Old policies retain four contexts; new diagnostics freeze up to twenty."""
    value=getattr(ops,'max_workers',4)
    value=value if type(value) is int and 1<=value<=20 else 4
    # Twenty outer scientific tasks already expose plenty of ready calls.
    # Do not multiply twenty nested pools into hundreds of waiting threads.
    return min(value,4) if nested else value


class Dispatch:
    """Cross-process request slots; legacy profiles retain their single slot."""
    def __init__(self,profile,root,deadline=None):
        self.profile=profile;self.root=Path(root);self.deadline=deadline or time.time()+7200
        self.lock_path=Path(profile['budget_database']+'.dispatch.lock')
        self.parallel=profile.get('request_scheduling')=='bounded_parallel_retry_after_v1'
        self.limit=profile['max_in_flight'] if self.parallel else 1
        if type(self.limit) is not int or not 1<=self.limit<=20:raise ValueError('Invalid request concurrency')

    def try_slot(self):
        if not self.parallel:paths=[self.lock_path]
        else:
            directory=Path(str(self.lock_path)+'.slots');directory.mkdir(parents=True,exist_ok=True)
            start=random.randrange(self.limit)
            paths=[directory/str((start+i)%self.limit) for i in range(self.limit)]
        for path in paths:
            handle=path.open('a')
            try:fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:handle.close()
            except BaseException:handle.close();raise
            else:return handle
        return None

    def check(self):
        if time.time()>=self.deadline:raise TimeoutError('Comparison evaluation deadline expired while waiting for OpenRouter')

    def notice(self,info):
        self.root.mkdir(parents=True,exist_ok=True)
        with (self.root/'transport_retries.jsonl').open('a') as out:
            out.write(json.dumps({'time':time.time(),'action':'WAIT_AND_RETRY_SAME_REQUEST',**info})+'\n')

    @contextmanager
    def locked(self):
        self.lock_path.parent.mkdir(parents=True,exist_ok=True)
        while True:
            self.check();handle=self.try_slot()
            if handle is not None:break
            time.sleep(.05)
        try:yield
        finally:fcntl.flock(handle,fcntl.LOCK_UN);handle.close()

    @asynccontextmanager
    async def alocked(self):
        self.lock_path.parent.mkdir(parents=True,exist_ok=True)
        while True:
            self.check();handle=self.try_slot()
            if handle is not None:break
            await asyncio.sleep(.05)
        try:yield
        finally:fcntl.flock(handle,fcntl.LOCK_UN);handle.close()

    def retry(self,error,attempt):
        info=inflight_throttle(error)
        if info is None and self.parallel and getattr(error,'status_code',None)==429:
            headers=getattr(getattr(error,'response',None),'headers',{}) or {}
            raw=headers.get('retry-after') or headers.get('Retry-After')
            try:delay=max(0,float(raw)) if raw is not None else min(2**min(attempt,6),60)
            except (TypeError,ValueError):delay=max(0,parsedate_to_datetime(raw).timestamp()-time.time())
            info={'reason':'rate_limited','limit_source':'provider_rate_limit','retry_after_seconds':delay}
        if info is not None and self.parallel:
            # Never retry before the server's interval; spread concurrent retries.
            info={**info,'retry_after_seconds':info['retry_after_seconds']+random.uniform(0,min(5,info['retry_after_seconds']*.1))}
        return info

    def run(self,call,*,sleep=time.sleep):
        if self.parallel:
            attempt=0
            while True:
                try:
                    with self.locked():self.check();return call()
                except Exception as error:
                    info=self.retry(error,attempt);attempt+=1
                    if info is None:raise
                    if time.time()+info['retry_after_seconds']>=self.deadline:
                        raise TimeoutError('Provider retry would exceed the evaluation deadline') from error
                    self.notice(info);sleep(info['retry_after_seconds'])
        with self.locked():
            while True:
                self.check()
                try:return call()
                except Exception as error:
                    info=inflight_throttle(error)
                    if info is None:raise
                    if time.time()+info['retry_after_seconds']>=self.deadline:
                        raise TimeoutError('OpenRouter admission retry would exceed the evaluation deadline') from error
                    self.notice(info);sleep(info['retry_after_seconds'])

    async def arun(self,call,*,sleep=asyncio.sleep):
        if self.parallel:
            attempt=0
            while True:
                try:
                    async with self.alocked():self.check();return await call()
                except Exception as error:
                    info=self.retry(error,attempt);attempt+=1
                    if info is None:raise
                    if time.time()+info['retry_after_seconds']>=self.deadline:
                        raise TimeoutError('Provider retry would exceed the evaluation deadline') from error
                    self.notice(info);await sleep(info['retry_after_seconds'])
        async with self.alocked():
            while True:
                self.check()
                try:return await call()
                except Exception as error:
                    info=inflight_throttle(error)
                    if info is None:raise
                    if time.time()+info['retry_after_seconds']>=self.deadline:
                        raise TimeoutError('OpenRouter admission retry would exceed the evaluation deadline') from error
                    self.notice(info);await sleep(info['retry_after_seconds'])
