"""Read-only health check for the active production package and its jobs."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from .production_reader import ProductionReleaseReader


def check_production_health(
    *, network: str, reader: ProductionReleaseReader, backend: Any,
    now: datetime | None = None, max_queued_seconds: int = 0,
    required_active_at: datetime | None = None,
) -> dict[str, Any]:
    """Return a small, secret-free health report; never changes job state."""
    clock = now or datetime.now(UTC)
    if (network not in {"mainnet", "testnet"} or clock.tzinfo is None
        or type(max_queued_seconds) is not int or max_queued_seconds < 0
        or (required_active_at is not None and required_active_at.tzinfo is None)):
        raise ValueError("invalid production health policy")
    active = reader.read_active()
    if active is None:
        if required_active_at is not None and clock >= required_active_at:
            return {"network": network, "status": "alert",
                    "alerts": ["missing_active_release"]}
        return {"network": network, "status": "inactive", "alerts": []}
    health = backend.production_health(
        network=network, receipt_sha256=active.receipt_sha256,
    )
    if (not isinstance(health, dict)
        or health.get("network") != network
        or health.get("receipt_sha256") != active.receipt_sha256):
        raise ValueError("production health differs from the active release")
    counts = {}
    for key in ("total", "queued", "leased", "expired_leases",
                "succeeded", "terminal_failures"):
        value = health.get(key)
        if type(value) is not int or value < 0:
            raise ValueError("production health contains an invalid count")
        counts[key] = value
    if (counts["queued"] + counts["leased"] + counts["succeeded"]
        + counts["terminal_failures"] != counts["total"]
        or counts["expired_leases"] > counts["leased"]):
        raise ValueError("production health counts do not reconcile")
    oldest = health.get("oldest_queued_at")
    if oldest is not None:
        if not isinstance(oldest, str):
            raise ValueError("production queue timestamp is invalid")
        try:
            queued_at = datetime.fromisoformat(oldest)
        except ValueError as exc:
            raise ValueError("production queue timestamp is invalid") from exc
        if queued_at.tzinfo is None:
            # SQLite CURRENT_TIMESTAMP is UTC but has no offset.
            queued_at = queued_at.replace(tzinfo=UTC)
        queued_age = max(0, int((clock - queued_at).total_seconds()))
    else:
        queued_age = None
    if (counts["queued"] == 0) != (queued_age is None):
        raise ValueError("production queue age differs from queued count")
    alerts = []
    if counts["terminal_failures"]:
        alerts.append("terminal_failures")
    if counts["expired_leases"]:
        alerts.append("expired_leases")
    if (max_queued_seconds and queued_age is not None
        and queued_age > max_queued_seconds):
        alerts.append("queue_age")
    return {
        "network": network, "status": "alert" if alerts else "healthy",
        "round_id": active.round_id, "receipt_sha256": active.receipt_sha256,
        **counts, "oldest_queued_age_seconds": queued_age,
        "alerts": alerts,
    }
