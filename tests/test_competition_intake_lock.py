import hashlib
from io import BytesIO

import pytest
from bittensor_wallet import Keypair

from competition.commitments import manifest_id
from competition.intake_lock import read_sealed_package, snapshot_round
from competition.policy import commitment_hash


def test_snapshot_round_reads_archive_at_deadline_and_sealed_package():
    miner = Keypair.create_from_mnemonic(Keypair.generate_mnemonic())
    package = b"sealed-wheel"
    salt = b"s" * 32
    manifest = {"entrypoint": "miner.cli:main"}
    digest = commitment_hash(package, miner.public_key, salt)
    upload = {
        "hotkey": miner.ss58_address,
        "status": "uploaded",
        "package_sha256": hashlib.sha256(package).hexdigest(),
        "package_size_bytes": len(package),
        "package_object_key": "sealed/testnet/week1/package.whl",
        "salt_hex": salt.hex(),
        "manifest": manifest,
    }

    class Archive:
        def get_block_hash(self, block):
            assert block == 456
            return "0x" + "a" * 64

        def get_commitment_metadata(self, netuid, hotkey_ss58, block=None):
            assert (netuid, hotkey_ss58, block) == (111, miner.ss58_address, 456)
            return {
                "block": 455,
                "info": {"fields": [
                    {"Sha256": "0x" + digest},
                    {"Raw64": "0x" + manifest_id(manifest).encode().hex()},
                ]},
            }

    class Store:
        def get_object(self, Bucket, Key):
            assert (Bucket, Key) == ("private", upload["package_object_key"])
            return {"ContentLength": len(package), "Body": BytesIO(package)}

    snapshot = snapshot_round(
        round_record={"status": "open", "deadline_block": 456},
        submissions=[upload], archive=Archive(), netuid=111,
        package_for_upload=lambda item: read_sealed_package(Store(), bucket="private", upload=item),
        qualified_hotkey=lambda hotkey: hotkey == miner.ss58_address,
    )
    assert snapshot.block_hash == "0x" + "a" * 64
    assert snapshot.selected[0]["qualified"] is True
    assert snapshot.selected[0]["commit_block"] == 455


def test_sealed_package_reader_fails_closed_on_size_and_hash():
    class Store:
        def get_object(self, Bucket, Key):
            return {"ContentLength": 3, "Body": BytesIO(b"bad")}

    upload = {
        "package_object_key": "sealed/week/p.whl", "package_size_bytes": 3,
        "package_sha256": hashlib.sha256(b"good").hexdigest(),
    }
    with pytest.raises(ValueError, match="differs"):
        read_sealed_package(Store(), bucket="private", upload=upload)
    with pytest.raises(ValueError, match="location"):
        read_sealed_package(Store(), bucket="private", upload={**upload, "package_object_key": "staging/p"})
