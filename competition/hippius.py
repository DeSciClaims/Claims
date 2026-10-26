"""Trusted private Hippius package and worker-log storage adapter."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any, Mapping

from .intake_lock import read_sealed_package


def hippius_s3_client(*, endpoint: str, access_key_id: str, secret_access_key: str) -> Any:
    if not endpoint.startswith("https://") or not access_key_id or not secret_access_key:
        raise ValueError("Hippius needs an HTTPS endpoint and private credentials")
    import boto3
    from botocore.config import Config

    return boto3.client(
        "s3", endpoint_url=endpoint,
        aws_access_key_id=access_key_id, aws_secret_access_key=secret_access_key,
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
    )


@dataclass(frozen=True)
class HippiusPrivateStorage:
    s3_client: Any = field(repr=False)
    package_bucket: str
    log_bucket: str
    max_log_bytes: int = 20_000_000

    def __post_init__(self) -> None:
        if not self.package_bucket or not self.log_bucket or self.max_log_bytes < 1:
            raise ValueError("private Hippius buckets and log limit are required")

    def package_for_submission(self, upload: Mapping[str, Any]) -> bytes:
        return read_sealed_package(self.s3_client, bucket=self.package_bucket, upload=upload)

    def put(self, key: str, content: bytes) -> None:
        if (not re.fullmatch(r"logs/(mainnet|testnet)/[A-Za-z0-9_-]{1,120}/"
                             r"[A-Za-z0-9]{20,80}/[0-9a-f]{64}/"
                             r"[0-9a-f]{64}-attempt-[12]\.txt", key)
            or not isinstance(content, bytes) or len(content) > self.max_log_bytes):
            raise ValueError("private worker log path or size is invalid")
        self.s3_client.put_object(
            Bucket=self.log_bucket, Key=key, Body=content,
            ContentType="text/plain; charset=utf-8",
            Metadata={"sha256": hashlib.sha256(content).hexdigest()},
        )
