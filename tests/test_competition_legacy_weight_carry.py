import copy

from bittensor_wallet import Keypair
import pytest

from competition.legacy_weight_carry import (
    LEGACY_WEIGHT_CARRY_SCHEMA,
    project_legacy_weights,
    seal_legacy_weights,
    verify_legacy_weights,
)
from competition.publication import canonical_sha256


@pytest.fixture(scope="module")
def keys():
    return [Keypair.create_from_mnemonic(Keypair.generate_mnemonic()) for _ in range(6)]


@pytest.fixture
def bundle(keys):
    signer, owner, operator, miner, other, _ = keys
    return {
        "schema_version": LEGACY_WEIGHT_CARRY_SCHEMA,
        "network": "mainnet", "netuid": 111,
        "validator_hotkey": signer.ss58_address,
        "snapshot_block": 500, "snapshot_block_hash": "a" * 64,
        "last_update_block": 490, "last_update_at": "2026-11-09T01:00:00+00:00",
        "entries": [
            {"hotkey": owner.ss58_address, "weight": 0.13},
            {"hotkey": operator.ss58_address, "weight": 0.51},
            {"hotkey": miner.ss58_address, "weight": 0.125},
            {"hotkey": other.ss58_address, "weight": 0.235},
        ],
        "provenance": {"source": "verified-chain-snapshot", "verified": True},
    }


def verify(receipt, keys, **overrides):
    return verify_legacy_weights(receipt, **{
        "expected_signer_hotkey": keys[0].ss58_address,
        "network": "mainnet", "netuid": 111, **overrides,
    })


def test_signed_carry_preserves_arbitrary_legacy_shares_and_detaches_input(bundle, keys):
    receipt = seal_legacy_weights(bundle, keys[0])
    assert verify(receipt, keys) == bundle
    assert receipt["bundle_sha256"] == canonical_sha256(bundle)
    assert Keypair(ss58_address=keys[0].ss58_address).verify(
        bytes.fromhex(receipt["bundle_sha256"]), bytes.fromhex(receipt["signature_hex"])
    )
    bundle["entries"][0]["weight"] = 1.0
    assert receipt["bundle"]["entries"][0]["weight"] == 0.13


def test_projection_uses_current_hotkeys_instead_of_historical_uids(bundle, keys):
    _, owner, operator, miner, other, new_miner = keys
    assert project_legacy_weights(bundle, owner_hotkey=owner.ss58_address,
        hotkeys_by_uid=[owner.ss58_address, other.ss58_address, operator.ss58_address,
                        new_miner.ss58_address, miner.ss58_address]) == {
            0: 0.13, 1: 0.235, 2: 0.51, 4: 0.125,
        }
    assert project_legacy_weights(bundle, owner_hotkey=owner.ss58_address,
        hotkeys_by_uid=[owner.ss58_address, operator.ss58_address, new_miner.ss58_address]) == {
            0: pytest.approx(0.49), 1: 0.51,
        }


def test_miner_only_carry_preserves_relative_miner_shares(bundle, keys):
    _, owner, operator, miner, other, _ = keys
    result = project_legacy_weights(
        bundle, owner_hotkey=owner.ss58_address,
        hotkeys_by_uid=[owner.ss58_address, operator.ss58_address,
                        miner.ss58_address, other.ss58_address],
        operator_hotkey=operator.ss58_address, miner_only=True,
    )
    assert result == pytest.approx({0: 0.13 / 0.49, 2: 0.125 / 0.49,
                                    3: 0.235 / 0.49})
    assert 1 not in result


def test_miner_only_carry_applies_base_burn(bundle, keys):
    _, owner, operator, miner, other, _ = keys
    result = project_legacy_weights(
        bundle, owner_hotkey=owner.ss58_address,
        hotkeys_by_uid=[owner.ss58_address, operator.ss58_address,
                        miner.ss58_address, other.ss58_address],
        operator_hotkey=operator.ss58_address, miner_only=True,
        base_burn=0.875758,
    )
    residual = 1 - 0.875758
    assert result == pytest.approx({0: 0.875758 + residual * 0.13 / 0.49,
                                    2: residual * 0.125 / 0.49,
                                    3: residual * 0.235 / 0.49})


@pytest.mark.parametrize("change", ["network", "netuid", "signer"])
def test_receipt_is_bound_to_the_validator_network_and_subnet(bundle, keys, change):
    receipt = seal_legacy_weights(bundle, keys[0])
    overrides = {"network": "testnet"} if change == "network" else (
        {"netuid": 530} if change == "netuid" else {"expected_signer_hotkey": keys[-1].ss58_address}
    )
    with pytest.raises(ValueError):
        verify(receipt, keys, **overrides)


def test_modified_digest_and_invalid_signatures_fail_closed(bundle, keys):
    receipt = seal_legacy_weights(bundle, keys[0])
    altered = copy.deepcopy(receipt)
    altered["bundle"]["provenance"]["source"] = "altered"
    with pytest.raises(ValueError, match="digest"):
        verify(altered, keys)
    altered["bundle_sha256"] = canonical_sha256(altered["bundle"])
    with pytest.raises(ValueError, match="signature"):
        verify(altered, keys)
    for signature in (None, "", "not-hex", "0" * 128):
        with pytest.raises(ValueError, match="signature"):
            verify({**receipt, "signature_hex": signature}, keys)


@pytest.mark.parametrize("weight", [0, -1, True, False, "0.13", float("nan"), float("inf"), -float("inf")])
def test_non_positive_or_non_numeric_weights_are_rejected(bundle, keys, weight):
    bundle["entries"][0]["weight"] = weight
    with pytest.raises(ValueError, match="weights"):
        seal_legacy_weights(bundle, keys[0])


def test_bad_total_duplicate_hotkeys_and_uid_only_entries_are_rejected(bundle, keys):
    for mutate in (
        lambda b: b["entries"][0].update(weight=0.2),
        lambda b: b["entries"][1].update(hotkey=b["entries"][0]["hotkey"]),
        lambda b: b["entries"][0].pop("hotkey"),
        lambda b: b["entries"][0].update(uid=0),
    ):
        altered = copy.deepcopy(bundle)
        mutate(altered)
        with pytest.raises(ValueError):
            seal_legacy_weights(altered, keys[0])


@pytest.mark.parametrize("field,value", [
    ("netuid", True), ("snapshot_block", True), ("snapshot_block", -1),
    ("snapshot_block", 489), ("last_update_block", "490"),
    ("snapshot_block_hash", "0x" + "a" * 64),
    ("last_update_at", "2026-11-09T01:00:00"), ("last_update_at", "invalid"),
    ("validator_hotkey", "raw-uid-1"), ("schema_version", "other"),
])
def test_malformed_evidence_cannot_be_sealed(bundle, keys, field, value):
    bundle[field] = value
    with pytest.raises(ValueError):
        seal_legacy_weights(bundle, keys[0])


def test_signer_must_match_the_captured_validator(bundle, keys):
    with pytest.raises(ValueError, match="signing hotkey"):
        seal_legacy_weights(bundle, keys[-1])


def test_missing_or_extra_fields_and_non_ss58_payees_are_rejected(bundle, keys):
    for mutate in (
        lambda b: b.update(raw_uids=[0, 1]),
        lambda b: b.pop("snapshot_block_hash"),
        lambda b: b["entries"][0].update(hotkey="invalid"),
    ):
        altered = copy.deepcopy(bundle)
        mutate(altered)
        with pytest.raises(ValueError):
            seal_legacy_weights(altered, keys[0])


@pytest.mark.parametrize("provenance", [
    None, {"raw": object()}, {"cost": float("nan")}, {"deep": {"a": {"b": {"c": 1}}}},
    {"large": "x" * 4097}, {"list": list(range(65))},
    {str(i): "x" * 2000 for i in range(5)},
])
def test_provenance_is_bounded_public_json(bundle, keys, provenance):
    bundle["provenance"] = provenance
    with pytest.raises(ValueError, match="provenance"):
        seal_legacy_weights(bundle, keys[0])


def test_provenance_is_optional_and_aware_z_timestamp_is_supported(bundle, keys):
    bundle.pop("provenance")
    bundle["last_update_at"] = "2026-11-09T01:00:00Z"
    assert verify(seal_legacy_weights(bundle, keys[0]), keys) == bundle


def test_projection_requires_verified_owner_slot_and_unique_current_hotkeys(bundle, keys):
    owner, other = keys[1].ss58_address, keys[-1].ss58_address
    for hotkeys, burn_uid in [([other, owner], 0), ([owner, owner], 0),
                              ([owner, other], True), ([owner, other], -1),
                              ([owner, other], 2), ([owner, "invalid"], 0)]:
        with pytest.raises(ValueError):
            project_legacy_weights(bundle, hotkeys_by_uid=hotkeys,
                                   owner_hotkey=owner, burn_uid=burn_uid)
