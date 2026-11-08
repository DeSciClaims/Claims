"""Six-hour carry-forward, publication handover and restart-safe submissions."""
import copy
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from bittensor_wallet import Keypair

from competition.legacy_weight_carry import seal_legacy_weights
from competition.publication import sign_publication
from competition.weight_heartbeat import WeeklyWeightHeartbeat
from test_competition_weight_heartbeat import receipt


class ClockChain:
    def __init__(self, signer, clock):
        self.clock = clock
        self.owner, self.operator, self.miner, self.runner = [
            Keypair.create_from_mnemonic(Keypair.generate_mnemonic()).ss58_address for _ in range(4)]
        self.block, self.calls, self.outcome = 1000, [], "success"
        self.timestamps = {100: clock[0] - timedelta(hours=6)}
        self.info = SimpleNamespace(
            hotkeys=[self.owner, signer.ss58_address, self.operator, self.miner, self.runner],
            owner_hotkey=self.owner, validator_permit=[False, True, False, False, False],
            last_update=[0, 100, 0, 0, 0], tempo=360)

    def get_metagraph_info(self, *, netuid):
        assert netuid == 111
        return self.info

    def get_current_block(self):
        return self.block

    def get_timestamp(self, *, block):
        return self.timestamps[block]

    def set_weights(self, **kwargs):
        self.calls.append(kwargs)
        if self.outcome == "rejected":
            return SimpleNamespace(success=False)
        if self.outcome == "timeout_unaccepted":
            raise TimeoutError("RPC disconnected")
        self.info.last_update[1] = self.block
        self.timestamps[self.block] = self.clock[0]
        if self.outcome == "timeout_accepted":
            raise TimeoutError("RPC disconnected after acceptance")
        return SimpleNamespace(success=True, data={"reveal_round": 123})


def setup(tmp_path):
    signer = Keypair.create_from_mnemonic(Keypair.generate_mnemonic())
    clock = [datetime(2026, 11, 9, 1, 6, 26, tzinfo=UTC)]
    chain = ClockChain(signer, clock)
    legacy = seal_legacy_weights({
        "schema_version": "claims_legacy_weight_carry_v1", "network": "mainnet", "netuid": 111,
        "validator_hotkey": signer.ss58_address, "snapshot_block": 1000,
        "snapshot_block_hash": "a" * 64, "last_update_block": 100,
        "last_update_at": chain.timestamps[100].isoformat(),
        "entries": [{"hotkey": chain.operator, "weight": .9},
                    {"hotkey": chain.miner, "weight": .1}],
    }, signer)
    path = tmp_path / "legacy.json"
    path.write_text(json.dumps(legacy))
    fetch = [None]

    def load():
        if isinstance(fetch[0], Exception):
            raise fetch[0]
        return fetch[0]

    def heartbeat(**overrides):
        return WeeklyWeightHeartbeat(
            network="mainnet", netuid=111, backend_url="https://api.example",
            operator_hotkey=chain.operator, wallet=SimpleNamespace(hotkey=signer), subtensor=chain,
            cache_path=tmp_path / "receipt.json", legacy_receipt_path=path,
            submission_interval_seconds=21600, now=lambda: clock[0], fetcher=load, **overrides)

    return signer, clock, chain, fetch, heartbeat


def competition_receipt(signer, chain):
    _, original = receipt()
    bundle = copy.deepcopy(original["bundle"])
    bundle["network"] = "mainnet"
    bundle["weights"]["roles"][0]["hotkey"] = chain.operator
    bundle["weights"]["roles"][1]["hotkey"] = chain.miner
    bundle["weights"]["roles"][2]["hotkey"] = chain.runner
    return sign_publication(bundle, signer)


def test_legacy_waits_six_hours_and_restores_cadence_after_restart(tmp_path):
    _, clock, chain, _, make = setup(tmp_path)
    clock[0] -= timedelta(seconds=1)
    heartbeat = make()
    preview = heartbeat.step(audit_only=True)
    assert preview["allocation_kind"] == "legacy_extraction"
    assert preview["weights"] == {2: .9, 3: .1}
    assert "round_id" not in preview and chain.calls == []
    assert heartbeat.step()["status"] == "waiting_for_interval"
    clock[0] += timedelta(seconds=1)
    assert heartbeat.step()["status"] == "submitted"
    assert chain.calls[-1]["period"] == 16  # transaction lifetime, not the cadence
    assert chain.calls[-1]["max_attempts"] == 1
    assert chain.info.tempo == 360
    assert heartbeat.state_path.stat().st_mode & 0o777 == 0o600
    clock[0] += timedelta(hours=6) - timedelta(seconds=1)
    chain.block += 1800
    restarted = make()
    assert restarted.step()["status"] == "waiting_for_interval"
    assert len(chain.calls) == 1
    clock[0] += timedelta(seconds=1)
    assert restarted.step()["status"] == "submitted"
    assert len(chain.calls) == 2


def test_first_competition_result_replaces_legacy_then_repeats_six_hourly(tmp_path):
    signer, clock, chain, fetch, make = setup(tmp_path)
    heartbeat = make()
    assert heartbeat.step()["allocation_kind"] == "legacy_extraction"
    fetch[0] = competition_receipt(signer, chain)
    clock[0] += timedelta(hours=1)
    chain.block += 300
    switched = heartbeat.step()
    assert switched["status"] == "submitted" and switched["allocation_kind"] == "competition"
    assert switched["weights"] == {0: .01, 2: .9, 3: .07, 4: .02}
    assert len(chain.calls) == 2
    clock[0] += timedelta(hours=5)
    fetch[0] = OSError("backend unavailable")
    assert make().step()["status"] == "waiting_for_interval"
    clock[0] += timedelta(hours=1)
    chain.block += 1800
    assert make().step()["status"] == "submitted"
    assert len(chain.calls) == 3


def test_backend_failure_before_publication_uses_signed_legacy(tmp_path):
    _, _, chain, fetch, make = setup(tmp_path)
    fetch[0] = OSError("backend unavailable")
    result = make().step()
    assert result["status"] == "submitted" and result["source"] == "legacy_after_fetch_error"
    assert len(chain.calls) == 1


def test_invalid_publication_fails_closed_instead_of_using_legacy(tmp_path):
    _, _, chain, fetch, make = setup(tmp_path)
    fetch[0] = {"invalid": True}
    with pytest.raises(RuntimeError, match="no valid weekly"):
        make().step()
    assert chain.calls == []


def test_competition_seen_by_audit_cannot_roll_back_if_cache_is_lost(tmp_path):
    signer, _, chain, fetch, make = setup(tmp_path)
    fetch[0] = competition_receipt(signer, chain)
    heartbeat = make()
    assert heartbeat.step(audit_only=True)["allocation_kind"] == "competition"
    heartbeat.cache_path.unlink()
    fetch[0] = None
    with pytest.raises(RuntimeError, match="refusing legacy rollback"):
        make().step()
    assert chain.calls == []


def test_ambiguous_accepted_rpc_is_reconciled_without_duplicate_commit(tmp_path):
    _, clock, chain, _, make = setup(tmp_path)
    chain.outcome = "timeout_accepted"
    with pytest.raises(TimeoutError):
        make().step()
    clock[0] += timedelta(minutes=1)
    chain.block += 5
    result = make().step()
    assert result["status"] == "waiting_for_interval"
    assert len(chain.calls) == 1
    assert "pending" not in json.loads(make().state_path.read_text())


def test_unknown_rpc_waits_for_mortality_window_before_retry(tmp_path):
    _, clock, chain, _, make = setup(tmp_path)
    chain.outcome = "timeout_unaccepted"
    with pytest.raises(TimeoutError):
        make().step()
    clock[0] += timedelta(minutes=1)
    chain.block += 5
    assert make().step()["status"] == "waiting_for_submission_confirmation"
    assert len(chain.calls) == 1
    chain.block += 32
    chain.outcome = "success"
    assert make().step()["status"] == "submitted"
    assert len(chain.calls) == 2


def test_rejected_submission_does_not_advance_cadence(tmp_path):
    _, clock, chain, _, make = setup(tmp_path)
    chain.outcome = "rejected"
    assert make().step()["status"] == "submission_failed"
    assert "last_submitted_at" not in json.loads(make().state_path.read_text())
    clock[0] += timedelta(minutes=1)
    chain.outcome = "success"
    assert make().step()["status"] == "submitted"


def test_carry_forward_burns_pruned_payee_instead_of_paying_reused_uid(tmp_path):
    _, _, chain, _, make = setup(tmp_path)
    chain.info.hotkeys[3] = Keypair.create_from_mnemonic(Keypair.generate_mnemonic()).ss58_address
    result = make().step()
    assert result["weights"] == {0: .1, 2: .9}


def test_carry_forward_rejects_unrelated_chain_update_before_first_submission(tmp_path):
    _, _, chain, _, make = setup(tmp_path)
    chain.info.last_update[1] = 101
    with pytest.raises(ValueError, match="changed after legacy capture"):
        make().step()
    assert chain.calls == []


def test_new_competition_does_not_need_a_pruned_previous_timestamp(tmp_path):
    signer, _, chain, fetch, make = setup(tmp_path)
    fetch[0] = competition_receipt(signer, chain)
    chain.timestamps.clear()
    result = make().step()
    assert result["status"] == "submitted" and result["allocation_kind"] == "competition"
    assert len(chain.calls) == 1


def test_pre_attempt_last_update_cannot_confirm_pending_submission(tmp_path):
    signer, clock, chain, fetch, make = setup(tmp_path)
    fetch[0] = competition_receipt(signer, chain)
    chain.outcome = "timeout_unaccepted"
    with pytest.raises(TimeoutError):
        make().step()
    chain.info.last_update[1] = 101  # later than the baseline, before attempt block1000
    chain.block += 5
    clock[0] += timedelta(minutes=1)
    assert make().step()["status"] == "waiting_for_submission_confirmation"
    assert "last_submitted_at" not in json.loads(make().state_path.read_text())
    chain.block += 32
    chain.outcome = "success"
    assert make().step()["status"] == "submitted"
    assert len(chain.calls) == 2


def test_recovery_without_timestamp_waits_from_reconciliation_time(tmp_path):
    _, clock, chain, _, make = setup(tmp_path)
    chain.outcome = "timeout_accepted"
    with pytest.raises(TimeoutError):
        make().step()
    chain.timestamps.clear()
    clock[0] += timedelta(hours=1)
    chain.block += 300
    result = make().step()
    assert result["status"] == "waiting_for_interval"
    assert datetime.fromisoformat(result["due_at"]) == clock[0] + timedelta(hours=6)
    assert len(chain.calls) == 1


def _competition_history(tmp_path):
    signer, clock, chain, fetch, make = setup(tmp_path)
    signed = competition_receipt(signer, chain)
    fetch[0] = signed
    assert make().step()["status"] == "submitted"
    clock[0] += timedelta(hours=1)
    chain.block += 300
    return signer, chain, fetch, make, signed


@pytest.mark.parametrize("order_change", [-1, 0])
def test_persisted_publication_order_rejects_rollback_when_cache_is_lost(tmp_path, order_change):
    signer, chain, fetch, make, signed = _competition_history(tmp_path)
    heartbeat = make()
    heartbeat.cache_path.unlink()
    conflicting = copy.deepcopy(signed["bundle"])
    conflicting["round_id"] = "older-or-conflicting-round"
    conflicting["drand_scheduled_at"] += order_change
    fetch[0] = sign_publication(conflicting, signer)
    with pytest.raises(RuntimeError, match="rollback"):
        make().step()
    state = json.loads(heartbeat.state_path.read_text())
    assert state["latest_competition_receipt_sha256"] == signed["bundle_sha256"]
    assert state["latest_competition_order"] == signed["bundle"]["drand_scheduled_at"]
    assert not heartbeat.cache_path.exists() and len(chain.calls) == 1


@pytest.mark.parametrize("legacy_state", [False, True])
def test_identical_competition_receipt_restores_lost_cache_without_submission(tmp_path, legacy_state):
    _, chain, _, make, signed = _competition_history(tmp_path)
    heartbeat = make()
    heartbeat.cache_path.unlink()
    if legacy_state:
        state = json.loads(heartbeat.state_path.read_text())
        state.pop("latest_competition_order")
        heartbeat.state_path.write_text(json.dumps(state))
    assert make().step()["status"] == "waiting_for_interval"
    assert json.loads(heartbeat.cache_path.read_text()) == signed
    state = json.loads(heartbeat.state_path.read_text())
    assert state["latest_competition_order"] == signed["bundle"]["drand_scheduled_at"]
    assert len(chain.calls) == 1


def test_newer_competition_receipt_can_replace_lost_cache(tmp_path):
    signer, chain, fetch, make, signed = _competition_history(tmp_path)
    heartbeat = make()
    heartbeat.cache_path.unlink()
    newer = copy.deepcopy(signed["bundle"])
    newer["round_id"] = "newer-round"
    newer["drand_scheduled_at"] += 1
    fetch[0] = sign_publication(newer, signer)
    result = make().step()
    assert result["status"] == "submitted" and result["round_id"] == "newer-round"
    assert json.loads(heartbeat.state_path.read_text())["latest_competition_order"] == newer["drand_scheduled_at"]
    assert len(chain.calls) == 2


def test_old_state_without_order_cannot_accept_different_receipt_after_cache_loss(tmp_path):
    signer, chain, fetch, make, signed = _competition_history(tmp_path)
    heartbeat = make()
    state = json.loads(heartbeat.state_path.read_text())
    state.pop("latest_competition_order")
    heartbeat.state_path.write_text(json.dumps(state))
    heartbeat.cache_path.unlink()
    newer = copy.deepcopy(signed["bundle"])
    newer["round_id"] = "unproven-newer-round"
    newer["drand_scheduled_at"] += 1
    fetch[0] = sign_publication(newer, signer)
    with pytest.raises(RuntimeError, match="unknown ordering"):
        make().step()
    assert not heartbeat.cache_path.exists() and len(chain.calls) == 1


def test_old_state_can_learn_order_from_its_existing_verified_cache(tmp_path):
    signer, chain, fetch, make, signed = _competition_history(tmp_path)
    heartbeat = make()
    state = json.loads(heartbeat.state_path.read_text())
    state.pop("latest_competition_order")
    heartbeat.state_path.write_text(json.dumps(state))
    older = copy.deepcopy(signed["bundle"])
    older["drand_scheduled_at"] -= 1
    fetch[0] = sign_publication(older, signer)
    result = make().step()
    assert result["status"] == "waiting_for_interval" and result["source"] == "cached_after_rollback"
    assert json.loads(heartbeat.state_path.read_text())["latest_competition_order"] == signed["bundle"]["drand_scheduled_at"]
    assert len(chain.calls) == 1


@pytest.mark.parametrize("order_change", [-1, 1])
def test_old_state_cannot_learn_order_from_a_different_cached_receipt(tmp_path, order_change):
    signer, chain, _, make, signed = _competition_history(tmp_path)
    heartbeat = make()
    state = json.loads(heartbeat.state_path.read_text())
    state.pop("latest_competition_order")
    heartbeat.state_path.write_text(json.dumps(state))
    different = copy.deepcopy(signed["bundle"])
    different["drand_scheduled_at"] += order_change
    heartbeat.cache_path.write_text(json.dumps(sign_publication(different, signer)))
    with pytest.raises(RuntimeError, match="persisted publication order"):
        make().step()
    assert len(chain.calls) == 1


def test_independent_heartbeat_persists_drand_round_order_after_cache_loss(tmp_path):
    from competition.replay_publication import sign_replay_publication
    from test_competition_weight_heartbeat import Chain, independent_receipt

    signer, signed = independent_receipt()
    chain = Chain(signer.ss58_address)
    fetch = [signed]

    def make():
        return WeeklyWeightHeartbeat(
            network="testnet", netuid=111, backend_url="https://api.example",
            operator_hotkey="operator", wallet=SimpleNamespace(hotkey=signer), subtensor=chain,
            cache_path=tmp_path / "independent.json", independent=True, fetcher=lambda: fetch[0],
        )

    heartbeat = make()
    assert heartbeat.step()["status"] == "submitted"
    assert json.loads(heartbeat.state_path.read_text())["latest_competition_order"] == signed["bundle"]["drand_round"]
    heartbeat.cache_path.unlink()
    older = copy.deepcopy(signed["bundle"])
    older["drand_round"] -= 1
    fetch[0] = sign_replay_publication(older, signer)
    with pytest.raises(RuntimeError, match="rollback"):
        make().step()
    assert len(chain.calls) == 1


@pytest.mark.parametrize("value", [True, 0, -1, 1.0, "21600"])
def test_interval_must_be_a_positive_integer(tmp_path, value):
    signer, _ = receipt()
    with pytest.raises(ValueError, match="positive integer"):
        WeeklyWeightHeartbeat(
            network="testnet", netuid=111, backend_url="https://api.example", operator_hotkey="operator",
            wallet=SimpleNamespace(hotkey=signer), subtensor=object(), cache_path=tmp_path / "r.json",
            submission_interval_seconds=value)
