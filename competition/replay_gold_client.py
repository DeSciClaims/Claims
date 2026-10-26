"""Open and read a separate signed Gold review without primary write access."""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Callable

from .publication import canonical_sha256
from .replay_gold_tally import tally_replay_gold_review


@dataclass(frozen=True)
class ReplayGoldClient:
    base_url: str
    replay_token: str
    signer: Any
    open_request: Callable[..., Any] = urllib.request.urlopen

    def __post_init__(self) -> None:
        url = urllib.parse.urlsplit(self.base_url)
        if (not url.hostname or url.username or url.password or url.query or url.fragment
            or not self.replay_token or not self.signer.ss58_address
            or (url.scheme != "https" and not (
                url.scheme == "http" and url.hostname in {"localhost", "127.0.0.1"}
            ))):
            raise ValueError("replay Gold needs a private HTTPS backend and signer")

    def _url(self, round_id: str, suffix: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,120}", round_id):
            raise ValueError("replay Gold round ID is invalid")
        return (self.base_url.rstrip("/") + "/internal/competition/replay-gold/rounds/"
                + urllib.parse.quote(round_id, safe="") + suffix)

    def open(self, round_id: str, *, review: dict[str, Any],
             voters: list[dict[str, str]]) -> dict[str, Any]:
        if review.get("round_id") != round_id or not voters:
            raise ValueError("replay Gold review or voters are missing")
        bundle = {"schema_version": "claims_replay_gold_open_v1",
                  "review": review, "voters": voters}
        digest = canonical_sha256(bundle)
        receipt = {"bundle": bundle, "bundle_sha256": digest,
                   "signer_hotkey": self.signer.ss58_address,
                   "signature_hex": self.signer.sign(bytes.fromhex(digest)).hex()}
        body = json.dumps(receipt, sort_keys=True, separators=(",", ":")).encode()
        if len(body) > 10_000_000:
            raise ValueError("replay Gold review exceeds ten MB")
        request = urllib.request.Request(
            self._url(round_id, "/open"), data=body, method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            with self.open_request(request, timeout=60) as response:
                raw = response.read(1_000_001)
        except (urllib.error.HTTPError, urllib.error.URLError, OSError) as exc:
            raise RuntimeError("replay Gold opening backend is unavailable") from exc
        if len(raw) > 1_000_000:
            raise ValueError("replay Gold opening response is too large")
        result = json.loads(raw)
        if (not isinstance(result, dict) or result.get("round_id") != round_id
            or result.get("review_sha256") != digest
            or result.get("status") not in {"open", "closed"}):
            raise ValueError("replay Gold backend acknowledged a different review")
        return result

    def closed(self, round_id: str, *, expected_review_sha256: str) -> dict[str, Any] | None:
        request = urllib.request.Request(
            self._url(round_id, "/closed"), method="GET",
            headers={"Authorization": f"Bearer {self.replay_token}"},
        )
        try:
            with self.open_request(request, timeout=30) as response:
                raw = response.read(15_000_001)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            raise RuntimeError("replay Gold backend is unavailable") from exc
        except (urllib.error.URLError, OSError) as exc:
            raise RuntimeError("replay Gold backend is unavailable") from exc
        if len(raw) > 15_000_000:
            raise ValueError("closed replay Gold review exceeds 15 MB")
        result = json.loads(raw)
        receipt = result.get("receipt") if isinstance(result, dict) else None
        if (not isinstance(result, dict) or result.get("round_id") != round_id
            or not isinstance(receipt, dict)
            or receipt.get("bundle_sha256") != expected_review_sha256
            or canonical_sha256(receipt.get("bundle")) != expected_review_sha256
            or receipt.get("signer_hotkey") != self.signer.ss58_address
            or not isinstance(result.get("votes"), list)):
            raise ValueError("closed replay Gold review differs from signed opening")
        from bittensor_wallet import Keypair

        try:
            signature = bytes.fromhex(receipt["signature_hex"])
            valid = Keypair(ss58_address=receipt["signer_hotkey"]).verify(
                bytes.fromhex(expected_review_sha256), signature,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("closed replay Gold opening signature is malformed") from exc
        if not valid:
            raise ValueError("closed replay Gold opening signature is invalid")
        bundle = receipt["bundle"]
        result["tally"] = tally_replay_gold_review(
            bundle["review"], voters=bundle["voters"], votes=result["votes"],
        )
        return result

    def close_when_due(self, round_id: str, *, closes_at: str) -> bool:
        try:
            deadline = datetime.fromisoformat(closes_at)
        except (TypeError, ValueError) as exc:
            raise ValueError("replay Gold close time is malformed") from exc
        if deadline.tzinfo is None:
            raise ValueError("replay Gold close time lacks a timezone")
        if datetime.now(UTC) < deadline.astimezone(UTC):
            return False
        request = urllib.request.Request(
            self._url(round_id, "/close"), data=b"", method="POST",
        )
        try:
            with self.open_request(request, timeout=30) as response:
                raw = response.read(1_000_001)
        except (urllib.error.HTTPError, urllib.error.URLError, OSError) as exc:
            raise RuntimeError("replay Gold closure backend is unavailable") from exc
        if len(raw) > 1_000_000:
            raise ValueError("replay Gold closure response is too large")
        result = json.loads(raw)
        if result.get("round_id") != round_id or result.get("status") != "closed":
            raise ValueError("replay Gold backend did not close the expected review")
        return True
