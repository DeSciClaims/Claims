"""Trusted control-plane client for maximhq/Bifrost virtual keys.

Targets the v1.5 governance API. Workers receive only the returned virtual
key and the inference /v1 URL, never this client or its management credential.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any


class BifrostError(RuntimeError):
    pass


@dataclass(frozen=True)
class PaperKey:
    key_id: str
    value: str
    expires_at: datetime
    token_limit: int


class BifrostClient:
    def __init__(self, *, management_url: str, admin_bearer: str | None = None) -> None:
        if not management_url.startswith(("http://127.0.0.1:", "http://localhost:", "https://")):
            raise ValueError("Bifrost management URL must be loopback or HTTPS")
        self.management_url = management_url.rstrip("/")
        self.admin_bearer = admin_bearer

    def _request(
        self, method: str, path: str, *, payload: dict[str, Any] | None = None,
        virtual_key: str | None = None,
    ) -> dict[str, Any]:
        headers = {"Content-Type": "application/json"}
        if virtual_key:
            headers["x-bf-vk"] = virtual_key
        elif self.admin_bearer:
            headers["Authorization"] = "Bearer " + self.admin_bearer
        request = urllib.request.Request(
            self.management_url + path,
            data=None if payload is None else json.dumps(payload, separators=(",", ":")).encode(),
            headers=headers,
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                body = response.read()
        except urllib.error.HTTPError as exc:
            raise BifrostError(f"Bifrost management request failed with HTTP {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise BifrostError("Bifrost management is unavailable") from exc
        return json.loads(body) if body else {}

    def create_paper_key(
        self,
        *, round_id: str, paper_id: str, hotkey: str,
        allowed_models: tuple[str, ...], token_limit: int,
        dollar_limit: float, expires_at: datetime,
    ) -> PaperKey:
        if not allowed_models or any(not model for model in allowed_models):
            raise ValueError("at least one allowed model is required")
        if token_limit < 1 or dollar_limit <= 0:
            raise ValueError("paper token and dollar limits must be positive")
        if expires_at.tzinfo is None or expires_at <= datetime.now(UTC):
            raise ValueError("paper key expiry must be a future timezone-aware timestamp")
        # The model list uses Bifrost's configured OpenRouter provider names.
        # OSS management stays on trusted loopback; the worker sees only /v1.
        response = self._request("POST", "/api/governance/virtual-keys", payload={
            "name": f"claims-{round_id}-{paper_id}-{hotkey[:12]}",
            "description": "Claims one-paper evaluation key",
            "provider_configs": [{
                "provider": "openrouter",
                "weight": 1,
                "allowed_models": list(allowed_models),
                "key_ids": ["*"],
            }],
            "budgets": [{"max_limit": dollar_limit, "reset_duration": "1h"}],
            "rate_limit": {
                "token_max_limit": token_limit,
                "token_reset_duration": "1h",
                "request_max_limit": 100,
                "request_reset_duration": "1m",
            },
            "expires_at": expires_at.astimezone(UTC).isoformat().replace("+00:00", "Z"),
            "is_active": True,
            "metadata": {
                "round_id": round_id,
                "paper_id": paper_id,
                "hotkey": hotkey,
            },
        })
        item = response.get("virtual_key") or {}
        key_id, value = item.get("id"), item.get("value")
        if not isinstance(key_id, str) or not isinstance(value, str) or not value.startswith("sk-bf-"):
            raise BifrostError("Bifrost did not return a usable virtual key")
        return PaperKey(key_id, value, expires_at, token_limit)

    def token_usage(self, key: PaperKey) -> int:
        # Self-service quota is scoped to this virtual key, including streams.
        response = self._request(
            "GET", "/api/governance/virtual-keys/quota", virtual_key=key.value,
        )
        rate_limit = response.get("rate_limit") or {}
        usage = rate_limit.get("token_current_usage")
        if not isinstance(usage, int) or usage < 0:
            raise BifrostError("Bifrost quota response lacks token usage")
        return usage

    def revoke(self, key: PaperKey) -> None:
        key_id = urllib.parse.quote(key.key_id, safe="")
        self._request("DELETE", f"/api/governance/virtual-keys/{key_id}")

    def check_budget(self, key: PaperKey) -> tuple[int, bool]:
        used = self.token_usage(key)
        return used, used > key.token_limit
