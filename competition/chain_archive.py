"""Bittensor archive-node adapter for deadline commitments and drand timing."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping


@dataclass(frozen=True)
class BittensorArchive:
    """Read raw CommitmentOf metadata at a historical block from Subtensor."""

    subtensor: Any

    def get_current_block(self) -> int:
        block = self.subtensor.get_current_block()
        if not isinstance(block, int) or block < 0:
            raise RuntimeError("archive node returned an invalid current block")
        return block

    def get_block_hash(self, block: int) -> str:
        if block < 0:
            raise ValueError("archive block must be nonnegative")
        value = self.subtensor.get_block_hash(block)
        if not isinstance(value, str) or not re.fullmatch(r"0x[0-9a-f]{64}", value):
            raise RuntimeError("archive node has no usable historical block hash")
        return value

    def get_commitment_metadata(
        self, netuid: int, hotkey_ss58: str, block: int | None = None,
    ) -> Mapping[str, Any] | str:
        if netuid < 0 or not hotkey_ss58 or block is None or block < 0:
            raise ValueError("commitment lookup requires a historical block and hotkey")
        value = self.subtensor.get_commitment_metadata(netuid, hotkey_ss58, block=block)
        if value == "":
            return ""
        if (not isinstance(value, Mapping)
            or not isinstance(value.get("block"), int)
            or value["block"] > block
            or not isinstance(value.get("info"), Mapping)):
            raise RuntimeError("archive commitment metadata is malformed or late")
        return value

    def get_timestamp(self, block: int) -> datetime:
        if block < 0:
            raise ValueError("archive block must be nonnegative")
        timestamp = self.subtensor.get_timestamp(block)
        if not isinstance(timestamp, datetime) or timestamp.tzinfo is None:
            raise RuntimeError("archive node returned a timestamp without timezone")
        return timestamp


class LazySubtensor:
    """Evaluation polling needs the backend, not a fresh chain connection."""
    def __init__(self, factory):
        self._factory = factory
        self._instance = None

    def __getattr__(self, name):
        if self._instance is None:
            self._instance = self._factory()
        return getattr(self._instance, name)

    def close(self):
        if self._instance is not None:
            self._instance.close()
            self._instance = None
