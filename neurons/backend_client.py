from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen


SIGNATURE_DOMAIN = "CLAIMS_VALIDATOR_REQUEST_V1"


class BackendClientError(RuntimeError):
    pass


@dataclass(frozen=True)
class ClaimsBackendClient:
    base_url: str
    wallet: Any
    network: str = "testnet"
    timeout_seconds: float = 30.0

    def post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        headers = {
            "content-type": "application/json",
            **self._signature_headers("POST", path, "", body),
        }
        request = Request(self._url(path), data=body, headers=headers, method="POST")
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                data = response.read()
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise BackendClientError(f"Backend POST {path} failed: {exc.code} {detail}") from exc
        except URLError as exc:
            raise BackendClientError(f"Backend POST {path} failed: {exc}") from exc
        if not data:
            return {}
        parsed = json.loads(data.decode("utf-8"))
        return parsed if isinstance(parsed, dict) else {"data": parsed}

    def select_batch(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self.post("/validator/batches/select", payload)

    def _url(self, path: str) -> str:
        return f"{self.base_url.rstrip('/')}/{path.lstrip('/')}"

    def _signature_headers(self, method: str, path: str, query: str, body: bytes) -> dict[str, str]:
        hotkey = self.wallet.hotkey.ss58_address
        timestamp = str(int(time.time()))
        nonce = uuid.uuid4().hex
        message = "\n".join(
            [
                SIGNATURE_DOMAIN,
                method.upper(),
                path,
                query,
                hotkey,
                timestamp,
                nonce,
                hashlib.sha256(body).hexdigest(),
            ]
        ).encode("utf-8")
        signature = self.wallet.hotkey.sign(message)
        signature_hex = signature.hex() if hasattr(signature, "hex") else bytes(signature).hex()
        return {
            "X-Claims-Hotkey": hotkey,
            "X-Claims-Timestamp": timestamp,
            "X-Claims-Nonce": nonce,
            "X-Claims-Signature": f"0x{signature_hex}",
            "X-Claims-Network": self.network,
        }


def path_from_url(url: str) -> str:
    parsed = urlparse(url)
    return parsed.path or "/"
