from __future__ import annotations

import re


_WS_RE = re.compile(r"\s+")


def normalize_text(value: str | None) -> str:
    return _WS_RE.sub(" ", (value or "").strip())
