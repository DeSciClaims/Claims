"""Process-local wire receipts and conservative spending reservations for replays."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from decimal import Decimal
import hashlib
import json
import math
from pathlib import Path
import threading
import time
from unittest.mock import patch
import uuid

import httpx


CURRENT_TRIAL: ContextVar[str | None] = ContextVar(
    "eligibility_benchmark_trial", default=None
)


def digest(value) -> str:
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, ensure_ascii=False, separators=(",", ":")
        ).encode()
    ).hexdigest()


def write_private(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    import os

    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as handle:
        json.dump(value, handle, ensure_ascii=False, sort_keys=True, default=str)
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)


class BudgetStopped(RuntimeError):
    pass


class LocalGuardRejected(ValueError):
    pass


class WireRecorder:
    """Reserve before EVERY physical HTTP attempt, including DSPy adapter fallbacks.

    No authentication headers are written. Unpriced/ambiguous attempts retain
    their full reservation across process restarts. This is a local conservative
    guard, not a replacement for a provider-enforced key spending limit.
    """

    def __init__(self, root: Path, config: dict):
        self.root = root
        self.config = config
        self.lock = threading.Lock()
        self.cap = Decimal(str(config["budget_usd"]))
        self.reserve = Decimal(str(config["request_reserve_usd"]))
        self.committed = Decimal("0")
        self.stopped = False
        self.denied_trials: set[str] = set()
        self.blocked_trials: dict[str, str] = {}
        self.rows: dict[str, dict] = {}
        for path in (root / "wire").glob("*.json"):
            row = json.loads(path.read_text())
            self.rows[row["attempt_id"]] = row
            self.committed += Decimal(
                str(
                    row.get("cost_usd")
                    if row.get("cost_usd") is not None
                    else row["reserved_usd"]
                )
            )
        if self.committed >= self.cap:
            self.stopped = True

    def begin(self, request: httpx.Request) -> tuple[httpx.Request, dict]:
        trial_id = CURRENT_TRIAL.get()
        if (
            not trial_id
            or request.method != "POST"
            or str(request.url) != "https://openrouter.ai/api/v1/chat/completions"
        ):
            self.reject(trial_id, "unapproved_or_unattributed_request")
        body = json.loads(request.content)
        allowed = {item["id"]: item for item in self.config["models"]}
        model = allowed.get(body.get("model"))
        if (
            model is None
            or body.get("stream")
            or body.get("tools")
            or body.get("plugins")
        ):
            self.reject(trial_id, "unconfigured_model_or_unsupported_request")
        limit = body.get("max_completion_tokens", body.get("max_tokens"))
        if type(limit) is not int or not 0 < limit <= model["max_tokens"]:
            self.reject(trial_id, "output_limit_not_preserved")
        # A deliberately loose byte-based prompt bound, with room for framing.
        prices = self.config["price_ceiling_usd_per_million"]
        bound = (
            (len(request.content) + 8192) * Decimal(str(prices["input"]))
            + limit * Decimal(str(prices["output"]))
        ) / Decimal(1_000_000)
        if bound > self.reserve:
            self.reject(trial_id, "request_exceeds_cost_reservation")
        provider = dict(body.get("provider") or {})
        caps = dict(provider.get("max_price") or {})
        for key, price in (
            ("prompt", prices["input"]),
            ("completion", prices["output"]),
        ):
            caps[key] = min(float(caps.get(key, price)), float(price))
        provider["max_price"] = caps
        body["provider"] = provider
        content = json.dumps(body).encode()
        headers = [
            (key, value)
            for key, value in request.headers.raw
            if key.lower() != b"content-length"
        ]
        forwarded = httpx.Request(
            request.method,
            request.url,
            headers=headers,
            content=content,
            extensions=request.extensions,
        )
        row = {
            "attempt_id": uuid.uuid4().hex,
            "trial_id": trial_id,
            "model": body["model"],
            "started_at": time.time(),
            "state": "in_flight",
            "reserved_usd": float(self.reserve),
            "request_sha256": digest(body),
            "output_limit": limit,
        }
        with self.lock:
            if self.stopped or self.committed + self.reserve > self.cap:
                self.stopped = True
                self.denied_trials.add(trial_id)
                raise BudgetStopped("benchmark spending reservation exhausted")
            self.committed += self.reserve
            self.rows[row["attempt_id"]] = row
            write_private(self.root / "wire" / (row["attempt_id"] + ".json"), row)
        return forwarded, row

    def reject(self, trial_id: str | None, reason: str):
        if trial_id:
            with self.lock:
                self.blocked_trials[trial_id] = reason
                write_private(
                    self.root / "blocked" / (trial_id + ".json"),
                    {"trial_id": trial_id, "reason": reason},
                )
        raise LocalGuardRejected(reason)

    def finish(
        self,
        row: dict,
        *,
        response: httpx.Response | None = None,
        error: Exception | None = None,
    ):
        updates = {
            "state": "finished",
            "duration_seconds": time.time() - row["started_at"],
            "ended_at": time.time(),
            "error_type": type(error).__name__ if error else None,
        }
        if response is not None:
            try:
                body = response.json()
            except (ValueError, UnicodeDecodeError):
                body = {}
            if not isinstance(body, dict):
                body = {}
            usage = body.get("usage") or {}
            usage = usage if isinstance(usage, dict) else {}
            cost = usage.get("cost")
            updates.update(
                http_status=response.status_code,
                retry_after=response.headers.get("retry-after"),
                provider=body.get("provider"),
                generation_id=body.get("id"),
                finish_reasons=[
                    c.get("finish_reason")
                    for c in body.get("choices", [])
                    if isinstance(c, dict)
                ],
                prompt_tokens=usage.get("prompt_tokens"),
                completion_tokens=usage.get("completion_tokens"),
                total_tokens=usage.get("total_tokens"),
                logical_error=bool(body.get("error")),
            )
            if type(cost) in (int, float) and math.isfinite(cost) and cost >= 0:
                updates["cost_usd"] = cost
            # Raw model content stays in this private output directory, not logs.
            write_private(self.root / "responses" / (row["attempt_id"] + ".json"), body)
        with self.lock:
            row.update(updates)
            if row.get("cost_usd") is not None:
                actual = Decimal(str(row["cost_usd"]))
                self.committed += actual - self.reserve
                if actual > self.reserve:
                    self.stopped = True
                    row["reservation_exceeded"] = True
            write_private(self.root / "wire" / (row["attempt_id"] + ".json"), row)

    def trial_rows(self, trial_id: str) -> list[dict]:
        with self.lock:
            return sorted(
                (dict(r) for r in self.rows.values() if r["trial_id"] == trial_id),
                key=lambda r: r["started_at"],
            )

    @contextmanager
    def intercept(self):
        """Catch SDK retries below DSPy, without changing the production runtime."""
        original = httpx.Client.send
        original_async = httpx.AsyncClient.send
        recorder = self

        def send(client, request, *args, **kwargs):
            forwarded, row = recorder.begin(request)
            try:
                response = original(client, forwarded, *args, **kwargs)
                response.read()
            except Exception as exc:
                recorder.finish(row, error=exc)
                raise
            recorder.finish(row, response=response)
            return response

        async def asend(client, request, *args, **kwargs):
            forwarded, row = recorder.begin(request)
            try:
                response = await original_async(client, forwarded, *args, **kwargs)
                await response.aread()
            except Exception as exc:
                recorder.finish(row, error=exc)
                raise
            recorder.finish(row, response=response)
            return response

        with (
            patch.object(httpx.Client, "send", send),
            patch.object(httpx.AsyncClient, "send", asend),
        ):
            yield self
