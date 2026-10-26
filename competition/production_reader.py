"""Read a verified active competition package from the trusted release store."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


@dataclass(frozen=True)
class ActiveProductionPackage:
    round_id: str
    receipt_sha256: str
    hotkey: str
    package_sha256: str
    activate_at: datetime
    latest_activation_at: datetime
    rolled_back_from: str | None
    package: bytes


class ProductionReleaseReader:
    """Read only the atomic pointer and its hash-verified package bytes.

    This reader never executes the package. A production runner can use the
    returned bytes only after applying its own worker isolation policy.
    """

    def __init__(self, root: Path, *, max_package_bytes: int = 100_000_000) -> None:
        if max_package_bytes < 1:
            raise ValueError("production package size limit must be positive")
        self.root = Path(root)
        self.max_package_bytes = max_package_bytes

    def _open_root(self) -> int:
        descriptor = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        info = os.fstat(descriptor)
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
            or info.st_mode & 0o077):
            os.close(descriptor)
            raise ValueError("production release root must be a private owned directory")
        return descriptor

    @staticmethod
    def _read_private_file(root_fd: int, name: str, limit: int) -> bytes:
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=root_fd)
        try:
            info = os.fstat(descriptor)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077 or info.st_size < 1 or info.st_size > limit):
                raise ValueError("production release file is unsafe or oversized")
            with os.fdopen(os.dup(descriptor), "rb") as content:
                data = content.read(limit + 1)
            if len(data) != info.st_size:
                raise ValueError("production release file changed during read")
            return data
        finally:
            os.close(descriptor)

    def read_active(self) -> ActiveProductionPackage | None:
        root_fd = self._open_root()
        try:
            try:
                pointer = self._read_private_file(root_fd, "active.json", 32_768)
            except FileNotFoundError:
                return None
            try:
                active = json.loads(pointer)
                if not isinstance(active, dict):
                    raise ValueError("production active pointer is malformed")
                digest = active["package_sha256"]
                receipt = active["receipt_sha256"]
                round_id = active["round_id"]
                hotkey = active["hotkey"]
                activate_at = datetime.fromisoformat(active["activate_at"])
                latest_activation_at = datetime.fromisoformat(
                    active.get("latest_activation_at", active["activate_at"]),
                )
                rolled_back_from = active.get("rolled_back_from")
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError("production active pointer is malformed") from exc
            if (not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)
                or not isinstance(receipt, str) or not re.fullmatch(r"[0-9a-f]{64}", receipt)
                or not isinstance(round_id, str) or not round_id
                or not isinstance(hotkey, str) or not hotkey
                or activate_at.tzinfo is None or latest_activation_at.tzinfo is None
                or latest_activation_at < activate_at
                or (rolled_back_from is not None and (not isinstance(rolled_back_from, str)
                    or not re.fullmatch(r"[0-9a-f]{64}", rolled_back_from)))):
                raise ValueError("production active pointer identity is invalid")
            package = self._read_private_file(
                root_fd, f"{digest}.zip", self.max_package_bytes,
            )
            if hashlib.sha256(package).hexdigest() != digest:
                raise ValueError("production package differs from active pointer")
            return ActiveProductionPackage(
                round_id, receipt, hotkey, digest, activate_at,
                latest_activation_at, rolled_back_from, package,
            )
        finally:
            os.close(root_fd)
