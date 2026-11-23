import io
import json

import pytest

from validator.scientific_result_v1.source_tasks.billing import (
    RecoveryBudget,
    reconcile_usage,
)
from validator.scientific_result_v1.source_tasks.operations import Hold
from validator.scientific_result_v1.source_tasks.store import Store


def test_unknown_reservations_survive_restart_and_cannot_be_treated_as_free(tmp_path):
    path=tmp_path/'budget.db';budget=RecoveryBudget(path,limit_usd=2,reserve_usd=1)
    a=budget.start('model','request');budget.finish(a,{})
    budget=RecoveryBudget(path,limit_usd=2,reserve_usd=1)
    b=budget.start('model','other');budget.finish(b,{'cost':.25})
    assert budget.summary()['known_usd']==.25 and budget.summary()['unresolved_reserved_usd']==1
    with pytest.raises(Hold):budget.start('model','no budget')
    with pytest.raises(ValueError):RecoveryBudget(path,limit_usd=3,reserve_usd=1)


def test_confirmed_zero_releases_reservation_and_inflight_crash_retains_it(tmp_path):
    budget=RecoveryBudget(tmp_path/'budget.db',limit_usd=1,reserve_usd=1)
    a=budget.start('model','request');budget.finish(a,{'cost':0})
    budget.start('model','crash')
    assert budget.summary()['known_usd']==0 and budget.summary()['in_flight_attempts']==1
    with pytest.raises(Hold):budget.start('model','retry')


def test_only_provider_bound_metadata_can_resolve_unknown_billing(tmp_path):
    store=Store(tmp_path/'evidence');old={'run_id':'r','model':'approved/model','billing_available':False,'provider_request_id':'gen-id','usage':{}}
    ref=store.put(old);store.advance('provider-usage:r',None,ref.sha256)
    def provider(request,**kwargs):
        assert request.full_url=='https://openrouter.ai/api/v1/generation?id=gen-id'
        return io.BytesIO(json.dumps({'data':{'id':'gen-id','model':'approved/model','total_cost':.05,'tokens_prompt':10,'tokens_completion':2}}).encode())
    result=reconcile_usage(store,'r','secret',opener=provider)
    assert result['cost_usd']==.05 and json.loads(store.get_blob(ref.sha256))==old
    updated=json.loads(store.get_blob(store.head('provider-usage:r')))
    assert updated['billing_available'] and updated['superseded_usage_sha256']==ref.sha256
    assert reconcile_usage(store,'r','secret',opener=provider)['status']=='already_observed'
    absent={**old,'run_id':'missing','provider_request_id':None};store.advance('provider-usage:missing',None,store.put(absent).sha256)
    def forbidden(*a,**k):raise AssertionError('Cannot invent a generation identity')
    assert reconcile_usage(store,'missing','secret',opener=forbidden)['status']=='unresolved'
