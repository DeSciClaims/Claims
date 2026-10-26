from datetime import UTC, datetime

import pytest

from competition.chain_archive import BittensorArchive
from competition.commitments import decode_fields


def test_archive_adapter_keeps_raw_commitment_fields_at_deadline():
    class Subtensor:
        def get_current_block(self):
            return 701

        def get_block_hash(self, block):
            assert block == 100
            return "0x" + "a" * 64

        def get_commitment_metadata(self, netuid, hotkey, block):
            assert (netuid, hotkey, block) == (111, "hotkey", 100)
            return {"block": 90, "info": {"fields": [
                {"Sha256": [1] * 32},
                {"Raw64": list(("b" * 64).encode())},
            ]}}

        def get_timestamp(self, block):
            assert block == 700
            return datetime(2026, 10, 26, 14, tzinfo=UTC)

    archive = BittensorArchive(Subtensor())
    assert archive.get_current_block() == 701
    assert archive.get_block_hash(100) == "0x" + "a" * 64
    assert decode_fields(archive.get_commitment_metadata(111, "hotkey", block=100)) == (
        bytes([1] * 32), "b" * 64, 90,
    )
    assert archive.get_timestamp(700).tzinfo is UTC


def test_archive_rejects_unavailable_or_late_historical_commitment():
    class Subtensor:
        def get_commitment_metadata(self, *_args, **_kwargs):
            return {"block": 101, "info": {"fields": []}}

    with pytest.raises(RuntimeError, match="late"):
        BittensorArchive(Subtensor()).get_commitment_metadata(111, "hotkey", block=100)
