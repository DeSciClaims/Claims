"""Deliver a separately signed replay receipt to the public competition API."""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable

from .replay_publication import verify_replay_publication


def _verify_hotkey_signature(hotkey: str, payload: bytes, signature: bytes) -> bool:
    from bittensor_wallet import Keypair

    return bool(Keypair(ss58_address=hotkey).verify(payload, signature))


@dataclass(frozen=True)
class ReplayPublicationClient:
    base_url: str
    expected_signer_hotkey: str
    open_request: Callable[..., Any] = urllib.request.urlopen

    def __post_init__(self) -> None:
        url = urllib.parse.urlsplit(self.base_url)
        if (not url.hostname or url.username or url.password or url.query or url.fragment
            or not self.expected_signer_hotkey
            or (url.scheme != "https" and not (
                url.scheme == "http" and url.hostname in {"localhost", "127.0.0.1"}
            ))):
            raise ValueError("replay publication needs an HTTPS backend URL and signer")

    def publish(
        self, round_id: str, receipt: dict[str, Any], *,
        verify_signature: Callable[[str, bytes, bytes], bool] = _verify_hotkey_signature,
    ) -> dict[str, Any]:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,120}", round_id):
            raise ValueError("replay publication round ID is invalid")
        bundle = verify_replay_publication(
            receipt, verify_signature=verify_signature,
        )
        if (bundle.get("round_id") != round_id
            or receipt["signer_hotkey"] != self.expected_signer_hotkey):
            raise ValueError("replay publication differs from expected round or signer")
        body = json.dumps(receipt, sort_keys=True, separators=(",", ":")).encode()
        if len(body) > 100_000:
            raise ValueError("replay publication exceeds 100 KB")
        url = (self.base_url.rstrip("/") + "/public/competition/rounds/"
               + urllib.parse.quote(round_id, safe="") + "/independent-results")
        request = urllib.request.Request(
            url, data=body, method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            with self.open_request(request, timeout=30) as response:
                raw = response.read(1_000_001)
        except (urllib.error.HTTPError, urllib.error.URLError, OSError) as exc:
            raise RuntimeError("replay publication backend is unavailable") from exc
        if len(raw) > 1_000_000:
            raise ValueError("replay publication response exceeds one MB")
        result = json.loads(raw)
        if (not isinstance(result, dict) or result.get("round_id") != round_id
            or result.get("bundle_sha256") != receipt["bundle_sha256"]
            or result.get("status") != "published"):
            raise ValueError("replay publication backend acknowledged a different receipt")
        return result
