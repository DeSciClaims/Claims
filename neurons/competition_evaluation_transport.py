"""HTTP transport for large paper receipts; scoring policy remains frozen."""

from __future__ import annotations

import gzip
import json
import urllib.error
import urllib.request
from typing import Any


class CompressedEvaluationTransport:
    """Compress evaluation uploads and request compact, compressed receipts."""

    def _request(
        self, method: str, path: str, payload: dict[str, Any] | None = None,
        *, not_found_none: bool = False, compress: bool = False,
    ) -> Any:
        if not path.endswith("/evaluation"):
            return super()._request(
                method, path, payload, not_found_none=not_found_none,
                compress=compress,
            )
        if method not in {"GET", "POST"} or (method == "POST") != (payload is not None):
            raise ValueError("invalid paper evaluation request")
        body = None
        if payload is not None:
            body = gzip.compress(
                json.dumps(payload, separators=(",", ":")).encode(),
                compresslevel=6, mtime=0,
            )
        request = urllib.request.Request(
            self.base_url + path + ("?compact=true" if method == "POST" else ""),
            data=body,
            headers={
                "Authorization": "Bearer " + self.controller_token,
                "Content-Type": "application/json",
                "Accept-Encoding": "gzip",
                **({"Content-Encoding": "gzip"} if body is not None else {}),
            },
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                data = response.read()
                encoding = response.headers.get("Content-Encoding", "identity").lower()
                if encoding == "gzip":
                    data = gzip.decompress(data)
                elif encoding != "identity":
                    raise RuntimeError("competition backend returned unsupported content encoding")
                return json.loads(data)
        except urllib.error.HTTPError as exc:
            if exc.code == 404 and not_found_none:
                return None
            raise RuntimeError(f"competition backend returned HTTP {exc.code}") from exc
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            raise RuntimeError("competition backend is unavailable") from exc
