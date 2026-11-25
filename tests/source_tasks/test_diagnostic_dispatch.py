import asyncio,json,threading,time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
import pytest
from validator.scientific_result_v1.source_tasks.diagnostic_dispatch import Dispatch,inflight_throttle
from validator.scientific_result_v1.source_tasks.proprietary_diagnostic import initialize_budget,policy,Budget


class Throttle(Exception):
    status_code=402
    body={'error':{'code':402,'metadata':{'reason':'in_flight_budget_exhausted','limit_source':'openrouter_in_flight_budget','headers':{'Retry-After':'120'}}}}


def profile(tmp_path):return policy('openai/gpt-5.5',context_window_tokens=1050000,budget_database=tmp_path/'budget.sqlite3',unlimited=True)


def test_transient_402_waits_server_interval_without_restarting_scientific_operation(tmp_path):
    calls=[];sleeps=[]
    def request():
        calls.append('same request')
        if len(calls)==1:raise Throttle()
        return 'valid response'
    assert Dispatch(profile(tmp_path),tmp_path).run(request,sleep=sleeps.append)=='valid response'
    assert calls==['same request','same request'] and sleeps==[120]
    log=json.loads((tmp_path/'transport_retries.jsonl').read_text())
    assert log['action']=='WAIT_AND_RETRY_SAME_REQUEST' and log['retry_after_seconds']==120


def test_generic_billing_402_is_not_retried(tmp_path):
    error=Throttle();error.body={'error':{'code':402,'metadata':{'reason':'insufficient_credits','limit_source':'openrouter_credits'}}}
    assert inflight_throttle(error) is None
    with pytest.raises(Throttle):Dispatch(profile(tmp_path),tmp_path).run(lambda:(_ for _ in ()).throw(error),sleep=lambda _:pytest.fail('must not wait'))


def test_litellm_wrapped_error_is_classified_by_exact_metadata():
    error=Exception('OpenrouterException - '+json.dumps(Throttle.body))
    assert inflight_throttle(error)['retry_after_seconds']==120
    assert inflight_throttle(Exception('in_flight_budget_exhausted')) is None


def test_native_requests_share_one_slot_between_harnesses(tmp_path):
    active=0;maximum=0;mutex=threading.Lock();p=profile(tmp_path)
    def job():
        nonlocal active,maximum
        def request():
            nonlocal active,maximum
            with mutex:active+=1;maximum=max(maximum,active)
            time.sleep(.03)
            with mutex:active-=1
        Dispatch(p,tmp_path).run(request)
    with ThreadPoolExecutor(max_workers=2) as executor:list(executor.map(lambda _:job(),range(2)))
    assert maximum==1


def test_async_native_request_honors_same_retry_after(tmp_path):
    calls=[];sleeps=[]
    async def request():
        calls.append(1)
        if len(calls)==1:raise Throttle()
        return 'done'
    async def sleep(delay):sleeps.append(delay)
    assert asyncio.run(Dispatch(profile(tmp_path),tmp_path).arun(request,sleep=sleep))=='done'
    assert sleeps==[120] and len(calls)==2


def test_spend_cap_can_be_removed_without_losing_cost_history(tmp_path):
    db=tmp_path/'budget.sqlite3';models=[{'id':'openai/gpt-5.5','pricing':{'prompt':'0.000005','completion':'0.00003'}}]
    b=initialize_budget(db,models,1)
    with pytest.raises(ValueError,match='spend cap'):b.reserve('openai/gpt-5.5',1000000,32768)
    b.remove_cap('owner requested no cap')
    token=b.reserve('openai/gpt-5.5',1000000,32768);b.settle(token,5)
    assert b.summary()['cap_usd'] is None and b.summary()['confirmed_usd']==5
    assert initialize_budget(db,models,None).summary()['confirmed_usd']==5


def test_queue_and_retry_after_do_not_trigger_hermes_idle_watchdog(tmp_path):
    from validator.scientific_result_v1.source_tasks.file_judge import profile_config
    settings=profile_config('openai/gpt-5.5',1050000,profile(tmp_path),operation_timeout=3600)
    assert settings['providers']['openrouter']=={'request_timeout_seconds':3600,'stale_timeout_seconds':3600}
    assert 'providers' not in profile_config('openai/gpt-5.5',1050000)


def parallel_profile(tmp_path,limit=20):
    return policy('openai/gpt-6-luna',context_window_tokens=1050000,
                  budget_database=tmp_path/'budget.sqlite3',unlimited=True,
                  max_output_tokens=65536,max_in_flight=limit,reasoning_effort='low')


def test_twenty_requests_can_run_and_twenty_first_waits(tmp_path):
    active=0;maximum=0;mutex=threading.Lock();full=threading.Event();release=threading.Event()
    p=parallel_profile(tmp_path)
    def job():
        def request():
            nonlocal active,maximum
            with mutex:
                active+=1;maximum=max(maximum,active)
                if active==20:full.set()
            try:assert release.wait(5)
            finally:
                with mutex:active-=1
        Dispatch(p,tmp_path).run(request)
    with ThreadPoolExecutor(max_workers=24) as executor:
        futures=[executor.submit(job)for _ in range(24)]
        try:
            assert full.wait(3), 'The twenty-request ceiling must be reachable'
            with mutex:assert active==20 and maximum==20
        finally:release.set()
        for future in futures:future.result()
    assert maximum==20


def test_async_requests_use_the_same_bounded_slots(tmp_path):
    p=parallel_profile(tmp_path,2);active=0;maximum=0
    async def job():
        async def request():
            nonlocal active,maximum
            active+=1;maximum=max(maximum,active)
            try:await asyncio.sleep(.02)
            finally:active-=1
        await Dispatch(p,tmp_path).arun(request)
    async def run():await asyncio.gather(*(job()for _ in range(5)))
    asyncio.run(run());assert maximum==2


def test_parallel_retry_honors_rate_limit_and_releases_slot_during_backoff(tmp_path):
    p=parallel_profile(tmp_path,1);calls=[];sleeps=[]
    error=Exception('rate limited');error.status_code=429
    error.response=SimpleNamespace(headers={'Retry-After':'10'})
    def request():
        calls.append(1)
        if len(calls)==1:raise error
        return 'done'
    def sleep(delay):
        sleeps.append(delay)
        with Dispatch(p,tmp_path,deadline=time.time()+1).locked():pass
    assert Dispatch(p,tmp_path).run(request,sleep=sleep)=='done'
    assert len(calls)==2 and 10<=sleeps[0]<=11


def _process_slot(profile,root,counts,mutex,full,release):
    def request():
        with mutex:
            counts[0]+=1;counts[1]=max(counts[1],counts[0])
            if counts[0]==2:full.set()
        try:
            if not release.wait(10):raise TimeoutError('Test release not received')
        finally:
            with mutex:counts[0]-=1
    Dispatch(profile,root).run(request)


def test_request_ceiling_is_shared_across_processes(tmp_path):
    import multiprocessing
    ctx=multiprocessing.get_context('spawn');counts=ctx.Array('i',2)
    mutex=ctx.Lock();full=ctx.Event();release=ctx.Event();p=parallel_profile(tmp_path,2)
    processes=[ctx.Process(target=_process_slot,args=(p,tmp_path,counts,mutex,full,release))for _ in range(3)]
    for child in processes:child.start()
    try:
        assert full.wait(8)
        with mutex:assert counts[0]==2 and counts[1]==2
    finally:
        release.set()
        for child in processes:child.join(10)
        for child in processes:
            if child.is_alive():child.terminate();child.join()
    assert all(child.exitcode==0 for child in processes)
    assert counts[1]==2
