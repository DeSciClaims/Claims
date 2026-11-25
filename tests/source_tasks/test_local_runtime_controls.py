import time
import pytest
from validator.scientific_result_v1.source_tasks.store import Store,Exhausted
from validator.scientific_result_v1.source_tasks.failure_tolerance import policy
from validator.scientific_result_v1.source_tasks.local_runtime_controls import validate
from validator.scientific_result_v1.source_tasks.operations import Hold

CONTROL={'kind':'claims_local_diagnostic_runtime_controls_v1','shared_max_llm_requests':500,'max_provider_attempts':1}


def test_no_retry_reuses_success_even_after_old_failed_attempt(tmp_path):
 s=Store(tmp_path);r=s.put({'request':1});answer=s.put({'answer':1})
 n,_=s.reserve('op',r.sha256);s.finish('op',n,error='old transient error')
 n,_=s.reserve('op',r.sha256);s.finish('op',n,response=answer.sha256)
 assert s.reserve('op',r.sha256,max_attempts=1)==(None,answer.sha256)


def test_no_retry_does_not_redispatch_existing_failure_or_uncertain_dispatch(tmp_path):
 s=Store(tmp_path);r=s.put({'request':1})
 n,_=s.reserve('failed',r.sha256);s.finish('failed',n,error='retained failure')
 s.reserve('uncertain',r.sha256,now=100)
 for op in ['failed','uncertain']:
  with pytest.raises(Exhausted,match='disabled retries'):s.reserve(op,r.sha256,max_attempts=1,now=1000)
 with s.connection() as c:assert c.execute('select count(*) from attempts').fetchone()[0]==2


def test_failed_runtime_is_called_once_and_stays_failed_on_resume(tmp_path,monkeypatch):
 from tests.source_tasks.test_docling_frame import fixture
 from validator.scientific_result_v1.source_tasks.docling_frame import EligibilityResponse
 _,_,ops=fixture(tmp_path,monkeypatch)
 ops.failure_tolerance_policy=policy();ops.diagnostic_runtime_controls=validate(CONTROL);ops.max_provider_attempts=1
 calls=[]
 class Broken:
  def generate(self,**kwargs):calls.append(1);raise TimeoutError('test network timeout')
 ops.runtimes['SOURCE_READER']=Broken()
 for _ in range(2):
  with pytest.raises(Hold):ops.request('SOURCE_READER','source_eligibility',{'tiles':[{'tile_id':'one'}]},EligibilityResponse)
 assert calls==[1]
 child=ops.scoped();assert child.max_provider_attempts==1


def test_maximum_is_500_and_controls_cannot_mutate_scientific_parameters():
 assert validate(CONTROL)==CONTROL
 for changed in [{'shared_max_llm_requests':501},{'max_provider_attempts':0},{'source_tokens':1}]:
  with pytest.raises(ValueError):validate({**CONTROL,**changed})


def test_implementation_amendment_preserves_other_scientific_identity_checks(tmp_path):
 from validator.scientific_result_v1.source_tasks.configuration import freeze_settings,MODELS
 from validator.scientific_result_v1.source_tasks.runner import make_operations
 s=Store(tmp_path/'s')
 snapshots={m:{'id':m,'architecture':{'input_modalities':['text']},'endpoints':[{'provider_name':'test','context_length':250000,'supported_parameters':['structured_outputs']}]} for m in set(MODELS.values())}
 settings,_=freeze_settings(s,route_snapshots=snapshots,execution_policy={'mode':'failure_tolerance_diagnostic','failure_tolerance_diagnostic':policy()})
 old=settings.model_copy(update={'implementation_digest':'a'*64})
 kwargs=dict(store=s,settings=old,policy_sha256='c'*64,trace_dir=tmp_path/'traces',offline=True)
 with pytest.raises(ValueError,match='implementation_digest'):make_operations(**kwargs,diagnostic_runtime_controls=CONTROL)
 amended={**CONTROL,'implementation_amendment':{'from_implementation_digest':'a'*64,'to_implementation_digest':settings.implementation_digest,'change_manifest_sha256':'b'*64}}
 ops=make_operations(**kwargs,diagnostic_runtime_controls=amended)
 assert ops.max_provider_attempts==1 and ops.policy_hash=='c'*64
 with pytest.raises(ValueError,match='renderer_digest'):
  make_operations(**{**kwargs,'settings':old.model_copy(update={'renderer_digest':'d'*64})},diagnostic_runtime_controls=amended)
