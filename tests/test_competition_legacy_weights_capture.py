import json
import sys
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from bittensor_wallet import Keypair

from competition.legacy_weight_carry import seal_legacy_weights, verify_legacy_weights
from neurons.competition_legacy_weights import capture_bundle, main, write_receipt


WHEN = datetime(2026, 11, 8, 19, 6, 26, 522695, tzinfo=UTC)


@pytest.fixture
def capture():
    keys = [Keypair.create_from_uri(uri) for uri in ("//Alice", "//Bob", "//Charlie")]
    hotkeys = [key.ss58_address for key in keys]
    evidence = {
        "schema_version": "claims_legacy_weight_evidence_v1", "network": "mainnet", "netuid": 111,
        "event": {"event_id": "weights_legacy", "run_id": "legacy", "network": "mainnet",
                  "status": "success", "created_at": WHEN.isoformat(), "operator_uid": 1,
                  "weights": [{"uid": 1, "weight": .9}, {"uid": 2, "weight": .1}]},
        "run": {"run_id": "legacy", "network": "mainnet", "validator_hotkey": hotkeys[0],
                "status": "completed", "target_miners": [{"uid": 2, "hotkey": hotkeys[2], "coldkey": "miner-coldkey"}]},
    }

    class Chain:
        raw = [(1, 65535), (2, 7282)]
        pending = [(1, [(hotkeys[2], 95, "DO_NOT_PUBLISH_CIPHERTEXT", 123)])]
        pruned = False
        closed = False

        def __init__(self):
            self.substrate = self
            self.info = SimpleNamespace(hotkeys=hotkeys, coldkeys=["validator", "operator", "miner-coldkey"],
                                        block_at_registration=[1, 4, 5])

        def get_chain_finalised_head(self):
            return "0x" + "a" * 64

        def get_block_number(self, value):
            assert value == "0x" + "a" * 64
            return 100

        def get_block_hash(self, value):
            assert value == 90
            return "0x" + "b" * 64

        def get_metagraph_info(self, **kwargs):
            assert kwargs == {"netuid": 111, "block": 100}
            return self.info

        def neuron_for_uid(self, **kwargs):
            assert kwargs == {"netuid": 111, "uid": 0, "block": 100}
            return SimpleNamespace(hotkey=hotkeys[0], last_update=90)

        def query(self, **kwargs):
            assert kwargs == {"module": "SubtensorModule", "storage_function": "Weights",
                              "params": [111, 0], "block_hash": "0x" + "a" * 64}
            return SimpleNamespace(value=self.raw)

        def query_map(self, **kwargs):
            assert kwargs["storage_function"] == "TimelockedWeightCommits"
            assert kwargs["params"] == [111] and kwargs["max_results"] == 257
            return self.pending

        def get_timestamp(self, *, block):
            if block == 90 and self.pruned:
                raise RuntimeError("UnknownBlock: State already discarded")
            return WHEN if block == 90 else WHEN + timedelta(minutes=1)

        def set_weights(self, **kwargs):
            raise AssertionError("capture must never submit weights")

        def close(self):
            self.closed = True

    return Chain(), evidence, keys


def bundle(capture, **kwargs):
    chain, evidence, keys = capture
    return capture_bundle(chain, network="mainnet", netuid=111,
                          validator_hotkey=keys[0].ss58_address, backend_evidence=evidence, **kwargs)


def test_finalized_allocation_is_identity_bound_and_signed_without_transactions(capture):
    result = bundle(capture)
    assert result["snapshot_block_hash"] == "a" * 64
    assert result["provenance"]["last_update_block_hash"] == "b" * 64
    assert result["provenance"]["timestamp_source"] == "chain_timestamp"
    assert result["provenance"]["pending_storage_groups_checked"] == 1
    assert "DO_NOT_PUBLISH_CIPHERTEXT" not in json.dumps(result)
    assert sum(row["weight"] for row in result["entries"]) == pytest.approx(1)
    assert result["entries"][0]["weight"] == pytest.approx(65535 / (65535 + 7282))
    receipt = seal_legacy_weights(result, capture[2][0])
    assert verify_legacy_weights(receipt, expected_signer_hotkey=capture[2][0].ss58_address,
                                 network="mainnet", netuid=111) == result


def test_reassigned_uid_is_rejected_before_sealing(capture):
    capture[0].info.block_at_registration[2] = 91
    with pytest.raises(ValueError, match="reassigned"):
        bundle(capture)


def test_pending_commit_in_a_later_group_blocks_capture(capture):
    capture[0].pending.append((2, [(capture[2][0].ss58_address, 99, "NEVER_LOG_THIS", 456)]))
    with pytest.raises(ValueError, match="pending timelocked"):
        bundle(capture)


def test_truncated_pending_query_fails_closed(capture):
    capture[0].pending = [(i, []) for i in range(257)]
    with pytest.raises(ValueError, match="safe bound"):
        bundle(capture)


@pytest.mark.parametrize("change", ["shares", "identity", "network", "validator", "status"])
def test_backend_evidence_must_corroborate_chain_allocation(capture, change):
    evidence = capture[1]
    if change == "shares":
        evidence["event"]["weights"] = [{"uid": 1, "weight": .8}, {"uid": 2, "weight": .2}]
    elif change == "identity":
        evidence["run"]["target_miners"][0]["hotkey"] = capture[2][1].ss58_address
    elif change == "network":
        evidence["run"]["network"] = "testnet"
    elif change == "validator":
        evidence["run"]["validator_hotkey"] = capture[2][1].ss58_address
    else:
        evidence["event"]["status"] = "failed"
    with pytest.raises(ValueError, match="(backend|identity)"):
        bundle(capture)


def test_pruned_timestamp_requires_explicit_matching_acknowledgement(capture):
    capture[0].pruned = True
    with pytest.raises(ValueError, match="last-update-at"):
        bundle(capture)
    with pytest.raises(ValueError, match="must equal"):
        bundle(capture, last_update_at=(WHEN - timedelta(hours=1)).isoformat(),
               timestamp_source_reason="RPC state pruned; authenticated backend success event")
    result = bundle(capture, last_update_at=WHEN.isoformat(),
                    timestamp_source_reason="RPC state pruned; authenticated backend success event")
    assert result["last_update_at"] == WHEN.isoformat()
    assert result["provenance"]["timestamp_source"] == "backend_success_event"


def test_available_chain_timestamp_cannot_be_overridden(capture):
    with pytest.raises(ValueError, match="conflicts"):
        bundle(capture, last_update_at=(WHEN - timedelta(minutes=1)).isoformat())


def test_capture_output_cannot_replace_an_existing_file_or_symlink(tmp_path):
    output = tmp_path / "carry.json"
    write_receipt(output, {"public": True})
    before = output.read_bytes()
    with pytest.raises(ValueError, match="already exists"):
        write_receipt(output, {"public": False})
    assert output.read_bytes() == before
    symlink = tmp_path / "linked.json"
    symlink.symlink_to(output)
    with pytest.raises(ValueError, match="already exists"):
        write_receipt(symlink, {"public": False})
    assert not list(tmp_path.glob(".legacy-weight-capture-*"))


def test_cli_only_accesses_hotkey_and_closes_chain(capture, tmp_path, monkeypatch):
    chain, evidence, keys = capture

    class Wallet:
        hotkey = keys[0]

        def __init__(self, **kwargs):
            assert kwargs == {"name": "owner", "hotkey": "validator", "path": "/wallets"}

        @property
        def coldkey(self):
            raise AssertionError("capture must not access the coldkey")

    monkeypatch.setitem(sys.modules, "bittensor", SimpleNamespace(Wallet=Wallet, Subtensor=lambda **_: chain))
    source, target = tmp_path / "evidence.json", tmp_path / "carry.json"
    source.write_text(json.dumps(evidence))
    assert main(["--network", "mainnet", "--netuid", "111", "--subtensor-network", "finney",
                 "--wallet-name", "owner", "--wallet-hotkey", "validator", "--wallet-path", "/wallets",
                 "--backend-evidence", str(source), "--output", str(target)]) == 0
    assert chain.closed
    verify_legacy_weights(json.loads(target.read_text()), expected_signer_hotkey=keys[0].ss58_address,
                          network="mainnet", netuid=111)
