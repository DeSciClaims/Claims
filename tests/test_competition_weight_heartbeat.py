import copy
import io
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from bittensor_wallet import Keypair

from competition.publication import publication_bundle, sign_publication
from competition.weight_heartbeat import (
    WeeklyWeightHeartbeat,
    fetch_latest_publication,
    project_weekly_weights,
    verify_weekly_receipt,
)


def receipt():
    signer = Keypair.create_from_mnemonic(Keypair.generate_mnemonic())
    round_record = {
        "round_id": "week2", "network": "testnet",
        "archive_block_hash": "0x" + "a" * 64, "drand_round": 10,
        "drand_seed": "b" * 64, "drand_chain_hash": "c" * 64,
        "drand_public_key": "d" * 96, "drand_signature": "e" * 192,
        "drand_scheduled_at": 1_800_000_000, "paper_pool_sha256": "f" * 64,
        "rules_sha256": "1" * 64, "configuration_sha256": "2" * 64,
        "baseline_package_sha256": "3" * 64,
    }
    bundle = publication_bundle(round_record, results={"decision": "new_crown"}, weights={
        "vector": [{"uid": 0, "weight": 0.01}, {"uid": 1, "weight": 0.07},
                   {"uid": 2, "weight": 0.02}, {"uid": 9, "weight": 0.90}],
        "roles": [
            {"hotkey": "operator", "role": "operator", "share": 0.90},
            {"hotkey": "king", "role": "production", "share": 0.07},
            {"hotkey": "runner", "role": "runner_up_1", "share": 0.02},
            {"hotkey": "", "role": "runner_up_2_burned", "share": 0.01},
        ],
        "burned_fraction": 0.01,
    })
    return signer, sign_publication(bundle, signer)


def test_signed_roles_reproject_after_uid_change_and_burn_unregistered_miner():
    signer, signed = receipt()
    bundle = verify_weekly_receipt(
        signed, expected_signer_hotkey=signer.ss58_address, network="testnet",
    )
    assert project_weekly_weights(
        bundle, hotkeys_by_uid=["owner", "king", "runner", "operator"],
        owner_hotkey="owner", operator_hotkey="operator",
    ) == {0: 0.01, 1: 0.07, 2: 0.02, 3: 0.90}
    assert project_weekly_weights(
        bundle, hotkeys_by_uid=["owner", "operator", "king"],
        owner_hotkey="owner", operator_hotkey="operator",
    ) == {0: 0.03, 1: 0.90, 2: 0.07}


def test_tampering_cross_network_and_unsafe_burn_fail_closed():
    signer, signed = receipt()
    with pytest.raises(ValueError, match="another network"):
        verify_weekly_receipt(signed, expected_signer_hotkey=signer.ss58_address, network="mainnet")
    tampered = copy.deepcopy(signed)
    tampered["bundle"]["weights"]["roles"][1]["share"] = 0.08
    with pytest.raises(ValueError, match="digest"):
        verify_weekly_receipt(tampered, expected_signer_hotkey=signer.ss58_address, network="testnet")
    bundle = signed["bundle"]
    with pytest.raises(ValueError, match="burn UID"):
        project_weekly_weights(bundle, hotkeys_by_uid=["stranger", "owner", "operator", "king"],
                               owner_hotkey="owner", operator_hotkey="operator")
    with pytest.raises(ValueError, match="operator"):
        project_weekly_weights(bundle, hotkeys_by_uid=["owner", "king", "runner"],
                               owner_hotkey="owner", operator_hotkey="operator")


def test_role_shares_must_match_weekly_policy_even_when_signed():
    signer, signed = receipt()
    modified = copy.deepcopy(signed["bundle"])
    modified["weights"]["roles"][1]["share"] = 0.08
    signed = sign_publication(modified, signer)
    with pytest.raises(ValueError, match="role or share"):
        verify_weekly_receipt(signed, expected_signer_hotkey=signer.ss58_address, network="testnet")


class Chain:
    def __init__(self, signer_hotkey):
        self.block = 500
        self.calls = []
        self.info = SimpleNamespace(
            hotkeys=["owner", signer_hotkey, "operator", "king", "runner"],
            owner_hotkey="owner", validator_permit=[False, True, False, False, False],
            last_update=[0, 0, 0, 0, 0], tempo=100,
        )

    def get_metagraph_info(self, *, netuid):
        assert netuid == 111
        return self.info

    def get_current_block(self):
        return self.block

    def set_weights(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(success=True)


def test_heartbeat_submits_every_tempo_and_reuses_verified_cache_on_backend_failure(tmp_path):
    signer, signed = receipt()
    chain = Chain(signer.ss58_address)
    wallet = SimpleNamespace(hotkey=signer)
    fetch = [signed]
    heartbeat = WeeklyWeightHeartbeat(
        network="testnet", netuid=111, backend_url="https://backend.example",
        operator_hotkey="operator", wallet=wallet, subtensor=chain,
        cache_path=tmp_path / "receipt.json", fetcher=lambda: fetch[0],
    )
    result = heartbeat.step()
    assert result["status"] == "submitted"
    assert result["weights"] == {0: 0.01, 2: 0.90, 3: 0.07, 4: 0.02}
    assert chain.calls[0]["uids"] == [0, 2, 3, 4]
    assert chain.calls[0]["period"] == 16
    chain.block = 550
    assert heartbeat.step()["status"] == "waiting_for_tempo"
    preview = heartbeat.step(audit_only=True)
    assert preview["status"] == "audit_only" and preview["weights"] == result["weights"]
    assert len(chain.calls) == 1
    fetch[0] = {"invalid": True}
    chain.block = 600
    result = heartbeat.step()
    assert result["status"] == "submitted" and result["source"] == "cached_after_fetch_error"
    assert len(chain.calls) == 2


def test_heartbeat_rejects_unsafe_chain_mapping_before_submission(tmp_path):
    signer, signed = receipt()
    chain = Chain(signer.ss58_address)
    chain.info.owner_hotkey = "wrong-owner"
    heartbeat = WeeklyWeightHeartbeat(
        network="testnet", netuid=111, backend_url="https://backend.example",
        operator_hotkey="operator", wallet=SimpleNamespace(hotkey=signer), subtensor=chain,
        cache_path=tmp_path / "receipt.json", fetcher=lambda: signed,
    )
    with pytest.raises(ValueError, match="burn UID"):
        heartbeat.step()
    assert chain.calls == []


def test_public_client_reads_latest_published_round_only():
    _, signed = receipt()
    urls = []

    def respond(url, *, timeout):
        assert timeout == 15
        urls.append(url)
        value = ([{"round_id": "week3", "status": "final"},
                  {"round_id": "week2", "status": "published"}]
                 if url.endswith("?network=testnet&limit=100") else signed)
        return io.BytesIO(json.dumps(value).encode())

    with patch("competition.weight_heartbeat.urllib.request.urlopen", side_effect=respond):
        assert fetch_latest_publication(
            backend_url="https://backend.example", network="testnet",
        ) == signed
    assert urls[1].endswith("/public/competition/rounds/week2/results")
