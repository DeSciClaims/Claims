from __future__ import annotations

import hashlib


def stable_id(prefix: str, *parts: str) -> str:
    joined = "||".join(parts)
    digest = hashlib.sha256(joined.encode("utf-8")).hexdigest()[:16]
    return f"{prefix}_{digest}"

