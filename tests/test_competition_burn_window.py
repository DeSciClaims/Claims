"""Signed-result activation, real payout arithmetic and two-week restoration."""
import copy
from datetime import timedelta
import json
from types import SimpleNamespace
from unittest.mock import patch
import urllib.error

import pytest

from competition.burn_window import BurnWindowPolicy
from competition.publication import sign_publication
from competition.weight_heartbeat import WeeklyWeightHeartbeat, fetch_round_publication, project_weekly_weights
from competition.weights import BASE_BURN, BURN_POLICY
from test_competition_weight_transition import setup, competition_receipt


def policy(**changes):
    return BurnWindowPolicy(**{**dict(schema_version='claims_burn_window_v1', network='mainnet', netuid=111,
        trigger_round_id='week2', base_burn_fraction=.3788, duration_seconds=14*86400, wait_for_trigger=True), **changes})


def pool_receipt(signer, chain, *, round_id='week2', order=1_800_000_000):
    bundle = copy.deepcopy(competition_receipt(signer, chain)['bundle'])
    bundle['round_id'], bundle['drand_scheduled_at'] = round_id, order
    residual = 1 - BASE_BURN
    bundle['weights'] = dict(allocation_policy=BURN_POLICY,
        vector=[{'uid':0,'weight':BASE_BURN+.1*residual},
                {'uid':3,'weight':.7*residual},{'uid':4,'weight':.2*residual}],
        roles=[{'hotkey':'','role':'base_burn_burned','share':BASE_BURN},
               {'hotkey':chain.miner,'role':'production','share':.7*residual},
               {'hotkey':chain.runner,'role':'runner_up_1','share':.2*residual},
               {'hotkey':'','role':'runner_up_2_burned','share':.1*residual}],
        burned_fraction=BASE_BURN+.1*residual)
    return sign_publication(bundle, signer)


def configured(tmp_path, **overrides):
    signer, clock, chain, fetch, _ = setup(tmp_path)
    signed = pool_receipt(signer, chain)
    trigger = [signed]
    fetch[0] = signed
    def make(**more):
        return WeeklyWeightHeartbeat(network='mainnet', netuid=111, backend_url='https://api.example',
            operator_hotkey=chain.operator, wallet=SimpleNamespace(hotkey=signer), subtensor=chain,
            cache_path=tmp_path/'receipt.json', submission_interval_seconds=21600,
            now=lambda:clock[0], fetcher=lambda:fetch[0], burn_window=policy(),
            burn_trigger_fetcher=lambda:trigger[0], **{**overrides, **more})
    return signer, clock, chain, fetch, trigger, make


def test_waits_for_exact_signed_result_without_chain_calls_or_starting_clock(tmp_path):
    _, clock, chain, fetch, trigger, make = configured(tmp_path)
    trigger[0] = None
    original = clock[0]
    for days in [0, 2, 9]:
        clock[0] = original + timedelta(days=days)
        result = make().step()
        assert result['status'] == 'waiting_for_burn_trigger'
        assert result['burn_window']['started_at'] is None
    assert not chain.calls
    assert not (tmp_path/'receipt.json.state.json').exists()
    assert not (tmp_path/'receipt.json').exists()


def test_full_window_starts_at_success_preserves_vacant_slot_and_restores_on_expiry(tmp_path):
    _, clock, chain, _, _, make = configured(tmp_path)
    heartbeat = make()
    first = heartbeat.step()
    assert first['status'] == 'submitted'
    assert first['weights'] == pytest.approx({0:.3788+.1*.6212, 3:.7*.6212, 4:.2*.6212})
    assert first['burn_window']['phase'] == 'active'
    start = clock[0]
    assert first['burn_window']['started_at'] == start.isoformat()
    assert first['burn_window']['expires_at'] == (start+timedelta(days=14)).isoformat()
    assert make().step()['status'] == 'waiting_for_interval'
    # Refresh just before expiry, then expiry must bypass the six-hour interval.
    clock[0] = start + timedelta(days=14, seconds=-1);chain.block += 1000
    near = make().step()
    assert near['status'] == 'submitted'
    assert near['burn_window']['expires_at'] == first['burn_window']['expires_at']
    clock[0] += timedelta(seconds=1);chain.block += 20
    restored = make().step()
    assert restored['status'] == 'submitted'
    assert restored['burn_window']['phase'] == 'expired'
    assert restored['weights'] == pytest.approx({0:BASE_BURN+.1*(1-BASE_BURN),
                                                3:.7*(1-BASE_BURN),4:.2*(1-BASE_BURN)})
    assert make().step()['status'] == 'waiting_for_interval'


def test_failed_submission_does_not_start_or_consume_window(tmp_path):
    _, clock, chain, _, _, make = configured(tmp_path)
    chain.outcome = 'rejected'
    result = make().step()
    assert result['status'] == 'submission_failed'
    assert result['burn_window']['started_at'] is None
    clock[0] += timedelta(days=2);chain.block += 100
    chain.outcome = 'success'
    result = make().step()
    assert result['burn_window']['started_at'] == clock[0].isoformat()
    assert result['burn_window']['expires_at'] == (clock[0]+timedelta(days=14)).isoformat()


def test_rpc_acceptance_with_lost_response_recovers_start_without_duplicate_submission(tmp_path):
    _, clock, chain, _, _, make = configured(tmp_path)
    chain.outcome = 'timeout_accepted'
    with pytest.raises(TimeoutError):
        make().step()
    accepted_at = clock[0]
    clock[0] += timedelta(minutes=1);chain.block += 5
    recovered = make().step()
    assert recovered['status'] == 'waiting_for_interval'
    assert recovered['burn_window']['started_at'] == accepted_at.isoformat()
    assert len(chain.calls) == 1


def test_audit_does_not_activate_window_or_submit(tmp_path):
    _, _, chain, _, _, make = configured(tmp_path)
    result = make().step(audit_only=True)
    assert result['status'] == 'audit_only'
    assert result['burn_window']['phase'] == 'awaiting_first_submission'
    assert not chain.calls
    assert 'burn_window' not in json.loads((tmp_path/'receipt.json.state.json').read_text())


def test_newer_receipts_change_recipients_without_resetting_window(tmp_path):
    signer, clock, chain, fetch, _, make = configured(tmp_path)
    first = make().step()
    new_chain = copy.copy(chain)
    new_chain.miner, new_chain.runner = chain.runner, chain.miner
    clock[0] += timedelta(days=1);chain.block += 100
    fetch[0] = pool_receipt(signer, new_chain, round_id='week3',order=1_800_000_100)
    result = make().step()
    assert result['status'] == 'submitted'
    assert result['weights'][4] == pytest.approx(.7*.6212)
    assert result['burn_window']['expires_at'] == first['burn_window']['expires_at']


def test_exact_result_seeds_cache_when_latest_listing_is_stale(tmp_path):
    signer, _, chain, fetch, _, make = configured(tmp_path)
    fetch[0] = pool_receipt(signer, chain, round_id='older', order=1_799_999_999)
    result = make().step()
    assert result['round_id'] == 'week2'
    assert result['status'] == 'submitted'


@pytest.mark.parametrize('bad', ['wrong_round','wrong_signature','unsupported_policy'])
def test_invalid_trigger_never_submits(tmp_path, bad):
    signer, _, chain, _, trigger, make = configured(tmp_path)
    if bad == 'wrong_round':
        trigger[0] = pool_receipt(signer,chain,round_id='wrong')
    elif bad == 'wrong_signature':
        trigger[0]['signature_hex'] = '00'*64
    else:
        trigger[0] = competition_receipt(signer,chain)
    with pytest.raises(ValueError):
        make().step()
    assert not chain.calls


@pytest.mark.parametrize('change', [dict(base_burn_fraction=True),dict(base_burn_fraction=1),
    dict(base_burn_fraction=float('nan')),dict(duration_seconds=0),dict(wait_for_trigger=1)])
def test_invalid_policy(change):
    with pytest.raises(ValueError):
        policy(**change)


def test_policy_and_state_cannot_reset_window_or_cross_network(tmp_path):
    _, clock, chain, _, _, make = configured(tmp_path)
    make().step()
    path=tmp_path/'receipt.json.state.json'
    state=json.loads(path.read_text())
    state['burn_window']['expires_at']=(clock[0]+timedelta(days=15)).isoformat()
    path.write_text(json.dumps(state))
    with pytest.raises(ValueError,match='expiry changed'):
        make().step()
    with pytest.raises(ValueError,match='another network'):
        WeeklyWeightHeartbeat(network='testnet',netuid=530,backend_url='https://api.example',
            operator_hotkey=chain.operator,wallet=SimpleNamespace(hotkey=SimpleNamespace(ss58_address='signer')),
            subtensor=chain,cache_path=tmp_path/'other.json',burn_window=policy())


def test_policy_removal_cannot_silently_remove_active_window(tmp_path):
    _, _, _, _, _, make = configured(tmp_path)
    heartbeat=make();heartbeat.step();heartbeat.burn_window=None
    with pytest.raises(ValueError,match='requires its policy'):
        heartbeat.step()


def test_owner_uid_validation_survives_temporary_override(tmp_path):
    signer, _, chain, _, _, _ = configured(tmp_path)
    receipt=pool_receipt(signer,chain)
    with pytest.raises(ValueError,match='current subnet owner'):
        project_weekly_weights(receipt['bundle'],hotkeys_by_uid=chain.info.hotkeys,
            owner_hotkey='wrong',operator_hotkey=chain.operator,base_burn_override=.3788)


def test_all_filled_roles_pay_exactly_6212_percent_of_miner_pool(tmp_path):
    signer, _, chain, _, _, _ = configured(tmp_path)
    signed=pool_receipt(signer,chain)
    bundle=copy.deepcopy(signed['bundle'])
    bundle['weights']['roles'][-1]={'hotkey':'third','role':'runner_up_2','share':.1*(1-BASE_BURN)}
    projected=project_weekly_weights(bundle,hotkeys_by_uid=chain.info.hotkeys+['third'],
        owner_hotkey=chain.owner,operator_hotkey=chain.operator,base_burn_override=.3788)
    assert projected == pytest.approx({0:.3788,3:.43484,4:.12424,5:.06212})
    assert sum(v for uid,v in projected.items() if uid != 0) == pytest.approx(.6212)


def test_ambiguous_cached_publication_cannot_activate_burn(tmp_path):
    signer, _, chain, _, _, make = configured(tmp_path)
    heartbeat=make()
    heartbeat._save_cache(pool_receipt(signer,chain,round_id='conflicting'))
    with pytest.raises(ValueError,match='conflicts with cached'):
        heartbeat.step()
    assert not chain.calls


def test_unpublished_endpoint_waits_but_auth_error_is_not_treated_as_pending():
    for code in [404,403]:
        with patch('urllib.request.urlopen',side_effect=urllib.error.HTTPError('https://api.example',code,'error',{},None)):
            if code == 404:
                assert fetch_round_publication(backend_url='https://api.example',round_id='week2') is None
            else:
                with pytest.raises(urllib.error.HTTPError):
                    fetch_round_publication(backend_url='https://api.example',round_id='week2')
