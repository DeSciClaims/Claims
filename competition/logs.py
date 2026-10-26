"""Upload one worker log to a private hotkey-scoped Hippius prefix."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class LogReceipt:
    object_key: str
    sha256: str
    size_bytes: int


def upload_worker_log(
    *, s3_client: Any, bucket: str, log_path: Path,
    network: str, round_id: str, hotkey: str, job_id: str,
    paper_key: str | None = None,
    max_log_bytes: int = 20_000_000,
) -> LogReceipt:
    if network not in {"testnet", "mainnet"} or not bucket:
        raise ValueError("invalid network or private log bucket")
    for label, value in (("round", round_id), ("hotkey", hotkey), ("job", job_id)):
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,120}", value):
            raise ValueError(f"invalid {label} identifier")
    if not log_path.is_file() or log_path.stat().st_size > max_log_bytes:
        raise ValueError("worker log is missing or exceeds size limit")
    data = log_path.read_bytes()
    if paper_key:
        data = data.replace(paper_key.encode(), b"[redacted-paper-key]")
    digest = hashlib.sha256(data).hexdigest()
    key = f"logs/{network}/{round_id}/{hotkey}/{job_id}.txt"
    s3_client.put_object(
        Bucket=bucket, Key=key, Body=data,
        ContentType="text/plain; charset=utf-8",
        Metadata={"sha256": digest},
    )
    return LogReceipt(key, digest, len(data))
