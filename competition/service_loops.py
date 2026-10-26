"""Restartable scheduling steps for trusted controller and worker processes.

The backend owns round and job state. A timer invokes the controller step; each
worker invokes the worker step with its own identity and disposable execution
adapter. Factories reconstruct credentials and frozen inputs outside miner VMs.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any, Callable, Mapping

from .weekly_automation import tick_round
from .worker_service import CompetitionWorkerCoordinator


def _safe_status(result: Mapping[str, Any]) -> str:
    status = result.get("status")
    if not isinstance(status, str) or not re.fullmatch(r"[a-z_]{1,80}", status):
        raise ValueError("competition step returned an invalid status")
    return status


def _pending(backend: Any, network: str) -> list[Mapping[str, Any]]:
    if network not in {"mainnet", "testnet"}:
        raise ValueError("unknown competition network")
    rows = backend.list_pending_rounds(network)
    if not isinstance(rows, list):
        raise ValueError("backend returned invalid pending rounds")
    seen: set[str] = set()
    for row in rows:
        if (not isinstance(row, Mapping) or not isinstance(row.get("round_id"), str)
            or not row["round_id"] or row["round_id"] in seen
            or row.get("network") != network or row.get("status") == "published"):
            raise ValueError("backend returned invalid pending round identity")
        seen.add(row["round_id"])
    return rows


def run_controller_cycle(
    *, backend: Any, network: str,
    inputs_for_round: Callable[[Mapping[str, Any]], Any],
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Advance every pending round once, isolating a fault to that round."""
    clock = now or datetime.now(UTC)
    if clock.tzinfo is None:
        raise ValueError("controller clock must have a timezone")
    results = []
    for row in _pending(backend, network):
        round_id = row["round_id"]
        try:
            inputs = inputs_for_round(row)
            if (inputs.evaluation.round_id != round_id
                or inputs.evaluation.configuration_sha256 != row["configuration_sha256"]
                or inputs.paper_inputs.backend is not backend):
                raise ValueError("controller factory differs from frozen round")
            result = tick_round(round_id, backend=backend, inputs=inputs, now=clock)
            results.append({"round_id": round_id, "status": _safe_status(result)})
        except Exception as exc:
            # Exception text can include provider responses or private data.
            results.append({"round_id": round_id, "status": "error",
                            "error_type": type(exc).__name__})
    return results


def run_worker_cycle(
    *, backend: Any, network: str,
    coordinator_for_round: Callable[[Mapping[str, Any]], CompetitionWorkerCoordinator],
) -> list[dict[str, Any]]:
    """Claim at most one job per active round from a dedicated worker."""
    results = []
    for row in _pending(backend, network):
        if row["status"] not in {"screening", "final"}:
            continue
        round_id = row["round_id"]
        try:
            coordinator = coordinator_for_round(row)
            if (coordinator.round_id != round_id or coordinator.network != network
                or coordinator.backend is not backend):
                raise ValueError("worker factory differs from pending round")
            result = coordinator.run_once()
            results.append({"round_id": round_id, "status": "idle"} if result is None
                           else {"round_id": round_id, "status": "job_processed",
                                 "worker_status": _safe_status(result)})
        except Exception as exc:
            results.append({"round_id": round_id, "status": "error",
                            "error_type": type(exc).__name__})
    return results
