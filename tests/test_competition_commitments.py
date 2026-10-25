import hashlib
import json

import pytest

from competition.commitments import (
    InvalidCommitment,
    build_archive_snapshot,
    decode_fields,
    manifest_id,
    verify_upload,
)
from competition.policy import commitment_hash


def fixture_upload(package=b"wheel-v1", *, hotkey="miner1", salt=b"s" * 32):
    manifest = {"entrypoint": "miner.cli:main", "models": ["model-x"]}
    upload = {
        "round_id": "week-1",
        "hotkey": hotkey,
        "status": "uploaded",
        "package_sha256": hashlib.sha256(package).hexdigest(),
        "package_size_bytes": len(package),
        "salt_hex": salt.hex(),
        "manifest": manifest,
    }
    digest = commitment_hash(package, b"public-key-32-bytes-of-miner-123", salt)
    record = {
        "block": 99,
        "info": {"fields": [
            {"Sha256": "0x" + digest},
            {"Raw64": "0x" + manifest_id(manifest).encode().hex()},
        ]},
    }
    return upload, record


def test_archive_verifies_exact_package_salt_hotkey_and_manifest():
    upload, record = fixture_upload()
    selected = verify_upload(
        upload, record, b"wheel-v1", deadline_block=100,
        hotkey_public_key=b"public-key-32-bytes-of-miner-123",
    )
    assert selected["commitment"] == record["info"]["fields"][0]["Sha256"][2:]
    assert selected["commit_block"] == 99
    assert len(selected) == 5
    assert manifest_id(upload["manifest"]) == hashlib.sha256(
        json.dumps(upload["manifest"], sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    with pytest.raises(InvalidCommitment):
        verify_upload(
            {**upload, "manifest": {"entrypoint": "other"}}, record, b"wheel-v1",
            deadline_block=100, hotkey_public_key=b"public-key-32-bytes-of-miner-123",
        )
    with pytest.raises(InvalidCommitment):
        verify_upload(
            upload, record, b"wheel-v2", deadline_block=100,
            hotkey_public_key=b"public-key-32-bytes-of-miner-123",
        )
    with pytest.raises(InvalidCommitment):
        verify_upload(
            upload, {**record, "block": 101}, b"wheel-v1", deadline_block=100,
            hotkey_public_key=b"public-key-32-bytes-of-miner-123",
        )


def test_snapshot_selects_only_matching_revision_and_keeps_qualification():
    old, _ = fixture_upload(b"old", salt=b"o" * 32)
    new, record = fixture_upload(b"new", salt=b"n" * 32)
    objects = {old["package_sha256"]: b"old", new["package_sha256"]: b"new"}
    snapshot = build_archive_snapshot(
        deadline_block=100, block_hash="0x" + "a" * 64,
        uploads=[old, new],
        commitment_for_hotkey=lambda _hotkey, block: record if block == 100 else None,
        package_for_upload=lambda upload: objects[upload["package_sha256"]],
        hotkey_public_key=lambda _hotkey: b"public-key-32-bytes-of-miner-123",
        qualified_hotkey=lambda _hotkey: False,
    )
    assert snapshot.block == 100
    assert len(snapshot.selected) == 1
    assert snapshot.selected[0]["package_sha256"] == new["package_sha256"]
    assert snapshot.selected[0]["qualified"] is False


def test_malformed_chain_layout_rejected_and_storage_fault_aborts():
    upload, record = fixture_upload()
    with pytest.raises(InvalidCommitment):
        decode_fields({**record, "info": {"fields": [{"Raw32": "0x" + "a" * 64}]}})
    with pytest.raises(OSError):
        build_archive_snapshot(
            deadline_block=100, block_hash="0x" + "a" * 64,
            uploads=[upload], commitment_for_hotkey=lambda *_: record,
            package_for_upload=lambda _: (_ for _ in ()).throw(OSError("Hippius unavailable")),
            hotkey_public_key=lambda _: b"public-key-32-bytes-of-miner-123",
            qualified_hotkey=lambda _: True,
        )
