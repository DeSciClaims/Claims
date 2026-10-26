"""Fetch a locked PDF in trusted compute before staging it on a miner VM."""

from __future__ import annotations

import hashlib
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable


class _AllowedRedirects(urllib.request.HTTPRedirectHandler):
    def __init__(self, validate_url: Callable[[str], None]) -> None:
        self.validate_url = validate_url

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        self.validate_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


@dataclass(frozen=True)
class TrustedPdfFetcher:
    """Allowlisted HTTPS or private S3 reads with size and SHA-256 checks."""

    allowed_https_hosts: tuple[str, ...] = ()
    allowed_s3_buckets: tuple[str, ...] = ()
    s3_client: Any = field(default=None, repr=False)
    hippius_endpoint: str = ""
    max_pdf_bytes: int = 100_000_000
    timeout_seconds: int = 60
    http_open: Callable[[str, int], Any] | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if self.max_pdf_bytes < 1 or self.timeout_seconds < 1:
            raise ValueError("PDF size and timeout limits must be positive")
        if any(not host or host != host.lower() or "/" in host
               for host in self.allowed_https_hosts):
            raise ValueError("HTTPS hosts must be explicit lowercase names")
        if any(not bucket or "/" in bucket for bucket in self.allowed_s3_buckets):
            raise ValueError("S3 buckets must be explicit names")
        if self.allowed_s3_buckets and self.s3_client is None:
            raise ValueError("private S3 buckets need a trusted client")
        if self.hippius_endpoint:
            parsed = urllib.parse.urlsplit(self.hippius_endpoint)
            if parsed.scheme != "https" or not parsed.hostname or parsed.path not in {"", "/"}:
                raise ValueError("Hippius endpoint must be an HTTPS origin")

    def _https_url(self, url: str) -> None:
        parsed = urllib.parse.urlsplit(url)
        if (parsed.scheme != "https" or parsed.username or parsed.password
            or parsed.port not in {None, 443} or parsed.fragment
            or parsed.hostname not in self.allowed_https_hosts):
            raise ValueError("PDF URL is outside the approved HTTPS hosts")

    def _s3_location(self, url: str) -> tuple[str, str] | None:
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme == "s3":
            if parsed.username or parsed.password or parsed.query or parsed.fragment:
                raise ValueError("private S3 URL has unsupported components")
            bucket, key = parsed.netloc, urllib.parse.unquote(parsed.path.lstrip("/"))
        elif self.hippius_endpoint and parsed.scheme == "https":
            endpoint = urllib.parse.urlsplit(self.hippius_endpoint)
            if parsed.netloc != endpoint.netloc:
                return None
            if parsed.query or parsed.fragment:
                raise ValueError("private Hippius URL has unsupported components")
            parts = parsed.path.lstrip("/").split("/", 1)
            bucket, key = parts if len(parts) == 2 else ("", "")
            key = urllib.parse.unquote(key)
        else:
            return None
        if (bucket not in self.allowed_s3_buckets or not key or key.startswith("/")
            or any(part in {".", ".."} for part in key.split("/"))):
            raise ValueError("PDF object is outside the approved S3 buckets")
        return bucket, key

    def _read(self, body: Any, *, declared_size: int | None = None) -> bytes:
        try:
            if declared_size is not None and declared_size > self.max_pdf_bytes:
                raise ValueError("PDF exceeds the configured size limit")
            content = body.read(self.max_pdf_bytes + 1)
        finally:
            body.close()
        if not isinstance(content, bytes) or not content or len(content) > self.max_pdf_bytes:
            raise ValueError("PDF is empty or exceeds the configured size limit")
        return content

    def fetch(self, url: str, expected_sha256: str) -> bytes:
        if not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
            raise ValueError("pinned PDF SHA-256 is invalid")
        location = self._s3_location(url)
        if location is not None:
            if self.s3_client is None:
                raise ValueError("private S3 client is unavailable")
            response = self.s3_client.get_object(Bucket=location[0], Key=location[1])
            length = response.get("ContentLength")
            content = self._read(response["Body"], declared_size=int(length) if length is not None else None)
        else:
            self._https_url(url)
            if self.http_open is None:
                opener = urllib.request.build_opener(_AllowedRedirects(self._https_url))
                response = opener.open(urllib.request.Request(url, headers={
                    "Accept": "application/pdf",
                }), timeout=self.timeout_seconds)
            else:
                response = self.http_open(url, self.timeout_seconds)
            self._https_url(response.geturl())
            length = response.headers.get("Content-Length")
            content = self._read(response, declared_size=int(length) if length else None)
        if hashlib.sha256(content).hexdigest() != expected_sha256:
            raise ValueError("PDF bytes differ from the locked source hash")
        return content
