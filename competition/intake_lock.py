"""Trusted controller adapter for locking uploads against block D.

The controller process holds archive-node, backend, and Hippius credentials.
None of these objects are available in the miner worker VM.
"""

from __future__ import annotations

import json
import hashlib
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Mapping, Protocol

from .commitments import ArchiveSnapshot, build_archive_snapshot


class ArchiveReader(Protocol):
    def get_block_hash(self, block: int) -> str: ...

    def get_commitment_metadata(
        self, netuid: int, hotkey_ss58: str, block: int | None = None,
    ) -> Mapping[str, Any] | str: ...


class CompetitionBackend:
    def __init__(self, *, base_url: str, controller_token: str) -> None:
        parsed = urllib.parse.urlparse(base_url)
        if parsed.scheme != "https" and parsed.hostname not in {"localhost", "127.0.0.1"}:
            raise ValueError("backend URL must be HTTPS or loopback")
        if not controller_token:
            raise ValueError("competition controller token is required")
        self.base_url = base_url.rstrip("/")
        self.controller_token = controller_token

    def _request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> Any:
        request = urllib.request.Request(
            self.base_url + path,
            data=None if payload is None else json.dumps(payload, separators=(",", ":")).encode(),
            headers={
                "Authorization": "Bearer " + self.controller_token,
                "Content-Type": "application/json",
            },
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"competition backend returned HTTP {exc.code}") from exc
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            raise RuntimeError("competition backend is unavailable") from exc

    def get_round(self, round_id: str) -> dict[str, Any]:
        return self._request("GET", "/internal/competition/rounds/" + urllib.parse.quote(round_id, safe=""))

    def list_submissions(self, round_id: str) -> list[dict[str, Any]]:
        path = "/internal/competition/rounds/" + urllib.parse.quote(round_id, safe="") + "/submissions"
        return self._request("GET", path)

    def lock_round(self, round_id: str, snapshot: ArchiveSnapshot) -> dict[str, Any]:
        path = "/internal/competition/rounds/" + urllib.parse.quote(round_id, safe="") + "/lock"
        return self._request("POST", path, {
            "archive_block": snapshot.block,
            "archive_block_hash": snapshot.block_hash,
            "selected": list(snapshot.selected),
        })


def read_sealed_package(
    s3_client: Any, *, bucket: str, upload: Mapping[str, Any],
    max_package_bytes: int = 100_000_000,
) -> bytes:
    """Read only the sealed object in a private receipt, bounded in memory."""
    key = str(upload["package_object_key"])
    if not key.startswith("sealed/") or ".." in key or not bucket:
        raise ValueError("invalid sealed package location")
    expected_size = int(upload["package_size_bytes"])
    if not 0 < expected_size <= max_package_bytes:
        raise ValueError("package exceeds controller size limit")
    response = s3_client.get_object(Bucket=bucket, Key=key)
    if int(response.get("ContentLength", -1)) != expected_size:
        raise ValueError("sealed package length differs from upload receipt")
    body = response["Body"]
    try:
        package = body.read(expected_size + 1)
    finally:
        body.close()
    if len(package) != expected_size or hashlib.sha256(package).hexdigest() != upload["package_sha256"]:
        raise ValueError("sealed package differs from upload receipt")
    return package


def snapshot_round(
    *, round_record: Mapping[str, Any], submissions: list[dict[str, Any]],
    archive: ArchiveReader, netuid: int,
    package_for_upload: Callable[[Mapping[str, Any]], bytes],
    qualified_hotkey: Callable[[str], bool],
) -> ArchiveSnapshot:
    """Build the lock payload from trusted archive and frozen qualification data."""
    if round_record["status"] != "open":
        raise ValueError("round must be open before lock")
    block = int(round_record["deadline_block"])
    if netuid < 0:
        raise ValueError("netuid must be nonnegative")
    block_hash = archive.get_block_hash(block)
    if not block_hash:
        raise RuntimeError("archive node has not finalized the deadline block")

    def public_key(hotkey: str) -> bytes:
        from bittensor_wallet import Keypair

        key = Keypair(ss58_address=hotkey).public_key
        if not isinstance(key, bytes) or len(key) != 32:
            raise ValueError("invalid hotkey public key")
        return key

    return build_archive_snapshot(
        deadline_block=block,
        block_hash=block_hash,
        uploads=submissions,
        commitment_for_hotkey=lambda hotkey, at_block: archive.get_commitment_metadata(
            netuid, hotkey, block=at_block,
        ),
        package_for_upload=package_for_upload,
        hotkey_public_key=public_key,
        qualified_hotkey=qualified_hotkey,
    )
