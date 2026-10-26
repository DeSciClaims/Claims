"""Discover private weekly replay receipts without changing systemd each week."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class ReplayRoundRegistry:
    factory: Any
    role: str
    backend_url: str
    rounds_dir: Path
    db_dir: Path
    worker_id: str | None = None
    runners: dict[str, tuple[str, Any]] = field(default_factory=dict)

    @staticmethod
    def _receipt_hash(path: Path) -> str:
        if (path.is_symlink() or not path.is_file()
            or path.stat().st_mode & 0o077):
            raise ValueError("replay receipt must be a private regular file")
        with path.open("rb") as handle:
            body = handle.read(1_000_001)
        if len(body) > 1_000_000:
            raise ValueError("replay receipt exceeds one MB")
        return hashlib.sha256(body).hexdigest()

    def __post_init__(self) -> None:
        if self.role not in {"controller", "worker"}:
            raise ValueError("replay registry role is invalid")
        for directory in (self.rounds_dir, self.db_dir):
            if (directory.is_symlink() or not directory.is_dir()
                or directory.stat().st_mode & 0o077):
                raise ValueError("replay registry directories must be private")

    def _runner(self, round_id: str, receipt_sha256: str) -> Any:
        cached = self.runners.get(round_id)
        if cached is not None:
            if cached[0] != receipt_sha256:
                raise ValueError("replay round receipt changed after startup")
            return cached[1]
        db_path = self.db_dir / f"{round_id}.sqlite"
        runner = (self.factory.create_replay_controller(
            round_id, self.backend_url, db_path,
        ) if self.role == "controller" else self.factory.create_replay_worker(
            round_id, self.backend_url, db_path, self.worker_id,
        ))
        self.runners[round_id] = receipt_sha256, runner
        return runner

    def run_once(self) -> list[dict[str, str]]:
        results = []
        for path in sorted(self.rounds_dir.glob("*.json")):
            round_id = path.stem
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,120}", round_id):
                continue
            try:
                digest = self._receipt_hash(path)
                runner = self._runner(round_id, digest)
                if self._receipt_hash(path) != digest:
                    raise ValueError("replay receipt changed during startup")
                result = runner.run_once()
                status = "idle" if result is None else result.get("status")
                if (not isinstance(status, str)
                    or not re.fullmatch(r"[a-z_]{1,80}", status)):
                    raise ValueError("replay step returned an invalid status")
                results.append({"round_id": round_id, "role": self.role,
                                "status": status})
            except Exception as exc:
                results.append({"round_id": round_id, "role": self.role,
                                "status": "error", "error_type": type(exc).__name__})
        return results
