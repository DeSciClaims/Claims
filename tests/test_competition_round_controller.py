import hashlib
from datetime import UTC, datetime

import pytest
from bittensor_wallet import Keypair

from competition.commitments import manifest_id
from competition.drand import DEFAULT_CHAIN_HASH, DEFAULT_PUBLIC_KEY, VerifiedBeacon
from competition.policy import commitment_hash
from competition.round_controller import lock_ready_round, reveal_ready_sample
from competition.sample_release import PaperRecord


def test_lock_freezes_qualified_archive_upload_and_releases_verified_sample(monkeypatch):
    miner = Keypair.create_from_mnemonic(Keypair.generate_mnemonic())
    package, salt = b"sealed-wheel", b"s" * 32
    manifest = {"team_id": "sample-team", "entrypoint": "miner.cli:main"}
    upload = {
        "hotkey": miner.ss58_address, "status": "uploaded",
        "package_sha256": hashlib.sha256(package).hexdigest(),
        "package_size_bytes": len(package), "salt_hex": salt.hex(),
        "manifest": manifest,
    }

    class Archive:
        def get_block_hash(self, block):
            assert block == 100
            return "0x" + "a" * 64

        def get_commitment_metadata(self, netuid, hotkey_ss58, block=None):
            assert (netuid, hotkey_ss58, block) == (111, miner.ss58_address, 100)
            return {"block": 99, "info": {"fields": [
                {"Sha256": "0x" + commitment_hash(package, miner.public_key, salt)},
                {"Raw64": "0x" + manifest_id(manifest).encode().hex()},
            ]}}

    class Backend:
        record = {"status": "open", "deadline_block": 100, "reveal_block": 700}
        receipt = None

        def get_round(self, round_id):
            assert round_id == "week1"
            return self.record

        def list_submissions(self, round_id):
            return [upload]

        def qualification_snapshot(self, round_id, hotkeys):
            assert hotkeys == [miner.ss58_address]
            return {"round_id": round_id, "hotkeys": [
                {"hotkey": miner.ss58_address, "qualified": True},
            ]}

        def lock_round(self, round_id, snapshot):
            assert snapshot.block == 100
            assert snapshot.selected[0]["qualified"] is True
            self.record = {**self.record, "status": "locked"}
            return {"round_id": round_id, "status": "locked"}

        def reveal_sample(self, round_id, receipt):
            self.receipt = receipt
            return {"round_id": round_id, "status": "revealed"}

    class Chain:
        height = 700

        def get_current_block(self):
            return self.height

        def get_timestamp(self, block):
            assert block == 700
            return datetime.fromtimestamp(1_800_000_000, tz=UTC)

    backend, chain = Backend(), Chain()
    assert lock_ready_round(
        "week1", backend=backend, archive=Archive(), netuid=111,
        package_for_upload=lambda item: package,
    )["status"] == "locked"
    signature = b"a" * 96
    beacon = VerifiedBeacon(
        123, hashlib.sha256(signature).digest(), signature.hex(),
        DEFAULT_CHAIN_HASH, DEFAULT_PUBLIC_KEY, 1_800_000_030,
    )
    monkeypatch.setattr("competition.round_controller.beacon_for_round", lambda record, *, chain: beacon)
    papers = [PaperRecord(f"paper-{index}", hashlib.sha256(str(index).encode()).hexdigest()) for index in range(4)]
    assert reveal_ready_sample(
        "week1", backend=backend, chain=chain, papers=papers,
        prescreen_count=0, screen_count=0, final_count=2, week_one=True,
    )["status"] == "revealed"
    assert backend.receipt["drand_signature"] == signature.hex()
    assert {item["stage"] for item in backend.receipt["papers"]} == {"week_one"}


def test_incomplete_qualification_aborts_before_archive_reads():
    class Backend:
        def get_round(self, round_id):
            return {"status": "open", "deadline_block": 100}

        def list_submissions(self, round_id):
            return [{"hotkey": "miner", "status": "uploaded"}]

        def qualification_snapshot(self, round_id, hotkeys):
            return {"round_id": round_id, "hotkeys": []}

    with pytest.raises(RuntimeError, match="incomplete"):
        lock_ready_round(
            "week1", backend=Backend(), archive=object(), netuid=111,
            package_for_upload=lambda item: b"bytes",
        )
