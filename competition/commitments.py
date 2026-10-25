"""Verify sealed submissions against the archive-node commitment at block D.

The on-chain field layout is ``[Sha256(h), Raw(manifest_id)]`` where
``h = sha256(package || hotkey_public_key || salt32)``. The manifest ID is
the SHA-256 hex digest of canonical JSON; miners commit its UTF-8 bytes.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from .policy import commitment_hash


class InvalidCommitment(ValueError):
    """A chain record or sealed upload cannot be used for this round."""


@dataclass(frozen=True)
class ArchiveSnapshot:
    block: int
    block_hash: str
    selected: tuple[dict[str, Any], ...]


def manifest_id(manifest: Mapping[str, Any]) -> str:
    canonical = json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _hex_bytes(value: Any) -> bytes:
    if isinstance(value, bytes):
        return value
    if isinstance(value, list) and all(isinstance(item, int) and 0 <= item <= 255 for item in value):
        return bytes(value)
    if isinstance(value, str):
        candidate = value.removeprefix("0x")
        try:
            return bytes.fromhex(candidate)
        except ValueError as exc:
            raise InvalidCommitment("commitment field is not hex") from exc
    raise InvalidCommitment("unsupported commitment field value")


def decode_fields(record: Mapping[str, Any]) -> tuple[bytes, str, int]:
    """Decode the raw SDK ``CommitmentOf`` response without UTF-8 first-field loss."""
    try:
        block = record["block"]
        fields = record["info"]["fields"]
        if not isinstance(block, int) or block < 0 or not isinstance(fields, list) or len(fields) != 2:
            raise InvalidCommitment("commitment has the wrong field layout")
        hash_field, manifest_field = fields
        if set(hash_field) != {"Sha256"} or len(hash_field) != 1:
            raise InvalidCommitment("first commitment field must be Sha256")
        digest = _hex_bytes(hash_field["Sha256"])
        if len(digest) != 32:
            raise InvalidCommitment("Sha256 field must contain 32 bytes")
        if len(manifest_field) != 1:
            raise InvalidCommitment("second commitment field must be Raw")
        raw_variant, raw_value = next(iter(manifest_field.items()))
        if not raw_variant.startswith("Raw"):
            raise InvalidCommitment("second commitment field must be Raw")
        raw = _hex_bytes(raw_value)
        manifest = raw.decode("ascii")
        if len(manifest) != 64 or any(char not in "0123456789abcdef" for char in manifest):
            raise InvalidCommitment("manifest ID must be lowercase SHA-256 hex")
    except (KeyError, TypeError, UnicodeDecodeError, AttributeError) as exc:
        raise InvalidCommitment("malformed commitment metadata") from exc
    return digest, manifest, block


def verify_upload(
    upload: Mapping[str, Any], record: Mapping[str, Any], package: bytes,
    *, deadline_block: int, hotkey_public_key: bytes,
) -> dict[str, Any]:
    digest, chain_manifest_id, commit_block = decode_fields(record)
    if commit_block > deadline_block:
        raise InvalidCommitment("commitment was included after the deadline")
    expected_package_hash = str(upload["package_sha256"])
    if hashlib.sha256(package).hexdigest() != expected_package_hash:
        raise InvalidCommitment("sealed package does not match its upload receipt")
    if len(package) != int(upload["package_size_bytes"]):
        raise InvalidCommitment("sealed package length does not match its upload receipt")
    try:
        salt = bytes.fromhex(str(upload["salt_hex"]))
    except ValueError as exc:
        raise InvalidCommitment("salt is not hex") from exc
    if len(salt) != 32:
        raise InvalidCommitment("salt must be 32 bytes")
    expected_h = commitment_hash(package, hotkey_public_key, salt)
    if digest.hex() != expected_h or chain_manifest_id != manifest_id(upload["manifest"]):
        raise InvalidCommitment("archive commitment does not match the sealed upload")
    return {
        "hotkey": str(upload["hotkey"]),
        "package_sha256": expected_package_hash,
        "salt_hex": salt.hex(),
        "commitment": digest.hex(),
        "commit_block": commit_block,
    }


def build_archive_snapshot(
    *, deadline_block: int, block_hash: str,
    uploads: list[dict[str, Any]],
    commitment_for_hotkey: Callable[[str, int], Mapping[str, Any] | None],
    package_for_upload: Callable[[Mapping[str, Any]], bytes],
    hotkey_public_key: Callable[[str], bytes],
    qualified_hotkey: Callable[[str], bool],
) -> ArchiveSnapshot:
    """Select only receipts matching each hotkey's last commitment at block D.

    The caller obtains ``block_hash`` and commitment records from an archive
    node at the same block, and verifies qualification from frozen vote data.
    Invalid or missing commitments are excluded. Storage outages must raise
    and abort the snapshot, rather than silently exclude a miner.
    """
    if deadline_block < 0 or not isinstance(block_hash, str) or not re.fullmatch(r"0x[0-9a-f]{64}", block_hash):
        raise ValueError("invalid archive block or block hash")
    grouped: dict[str, list[dict[str, Any]]] = {}
    for upload in uploads:
        if upload.get("status") != "uploaded":
            continue
        grouped.setdefault(str(upload["hotkey"]), []).append(upload)
    selected: list[dict[str, Any]] = []
    for hotkey in sorted(grouped):
        record = commitment_for_hotkey(hotkey, deadline_block)
        if not record:
            continue
        matches: list[dict[str, Any]] = []
        public_key = hotkey_public_key(hotkey)
        for upload in grouped[hotkey]:
            # The exact bytes need fetching to authenticate a receipt. Fetch
            # errors propagate; an unavailable object is an infrastructure fault.
            package = package_for_upload(upload)
            try:
                match = verify_upload(
                    upload, record, package, deadline_block=deadline_block,
                    hotkey_public_key=public_key,
                )
            except InvalidCommitment:
                continue
            matches.append(match)
        if len(matches) > 1:
            raise InvalidCommitment(f"ambiguous matching uploads for {hotkey}")
        if matches:
            selected.append({**matches[0], "qualified": qualified_hotkey(hotkey)})
    return ArchiveSnapshot(deadline_block, block_hash, tuple(selected))
