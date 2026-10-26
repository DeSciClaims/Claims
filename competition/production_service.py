"""Restartable, signed Monday handoff into the private production release store."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Callable, Mapping

from .handoff_controller import activate_published_round
from .production_handoff import FilesystemProductionReleaseStore
from .production_reader import ProductionReleaseReader


@dataclass
class ProductionHandoffService:
    backend: Any
    network: str
    expected_signer_hotkey: str
    sealed_packages: Any
    releases: FilesystemProductionReleaseStore
    reader: ProductionReleaseReader
    publication_for_round: Callable[[str], Mapping[str, Any]]
    reference_upload_for_round: Callable[[str], Mapping[str, Any] | None]
    _handled_without_change: set[str] = field(default_factory=set, repr=False)

    def run_once(self, *, now: datetime | None = None) -> list[dict[str, Any]]:
        """Process due published rounds oldest first; stop on a failed handoff."""
        clock = now or datetime.now(UTC)
        if clock.tzinfo is None or self.network not in {"testnet", "mainnet"}:
            raise ValueError("handoff needs an aware clock and known network")
        rows = self.backend.list_published_rounds(self.network)
        if not isinstance(rows, list):
            raise ValueError("backend returned invalid published rounds")
        due: list[tuple[datetime, str]] = []
        seen: set[str] = set()
        for row in rows:
            if (not isinstance(row, dict) or row.get("network") != self.network
                or row.get("status") != "published"
                or not isinstance(row.get("round_id"), str)
                or not row["round_id"] or row["round_id"] in seen):
                raise ValueError("published round list has invalid identity")
            seen.add(row["round_id"])
            deadline = datetime.fromisoformat(str(row.get("deadline_at")))
            if deadline.tzinfo is None:
                raise ValueError("published round has no aware deadline")
            utc_deadline = deadline.astimezone(UTC)
            if (utc_deadline.weekday(), utc_deadline.hour, utc_deadline.minute,
                utc_deadline.second) != (0, 12, 0, 0):
                raise ValueError("published round has no Monday noon deadline")
            gate = utc_deadline + timedelta(days=7, hours=-3)
            due.append((gate, row["round_id"]))
        due.sort()
        active = self.reader.read_active()
        latest = active.latest_activation_at if active else None
        results: list[dict[str, Any]] = []
        for gate, round_id in due:
            if clock.astimezone(UTC) < gate:
                continue
            if latest is not None and gate <= latest:
                continue
            if round_id in self._handled_without_change:
                continue
            try:
                activation = activate_published_round(
                    round_id, backend=self.backend,
                    publication_for_round=self.publication_for_round,
                    sealed_packages=self.sealed_packages, releases=self.releases,
                    expected_signer_hotkey=self.expected_signer_hotkey,
                    network=self.network, now=clock,
                    reference_upload=self.reference_upload_for_round(round_id),
                )
                if activation is None:
                    if active is None:
                        raise ValueError("continuing production has no active release")
                    self._handled_without_change.add(round_id)
                    results.append({"round_id": round_id, "status": "no_package_change",
                                    "active_release_present": active is not None})
                    continue
                active = self.reader.read_active()
                if (active is None or active.receipt_sha256 != activation["receipt_sha256"]
                    or active.package_sha256 != activation["package_sha256"]
                    or active.latest_activation_at != gate):
                    raise ValueError("handoff release pointer differs from activated package")
                latest = active.latest_activation_at
                results.append({"round_id": round_id, "status": "activated",
                                "receipt_sha256": active.receipt_sha256,
                                "package_sha256": active.package_sha256})
            except Exception as exc:
                results.append({"round_id": round_id, "status": "error",
                                "error_type": type(exc).__name__})
                break
        return results
