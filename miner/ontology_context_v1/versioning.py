from __future__ import annotations

import re


DEFAULT_RUN_LABEL = "default"
RUN_SUFFIX_PREFIX = "__run_"


def normalize_run_label(value: str | None) -> str:
    raw = (value or "").strip()
    if not raw:
        return DEFAULT_RUN_LABEL
    normalized = re.sub(r"[^A-Za-z0-9._-]+", "-", raw).strip("-._")
    return normalized or DEFAULT_RUN_LABEL


def versioned_name(base_name: str, run_label: str | None) -> str:
    normalized = normalize_run_label(run_label)
    if normalized == DEFAULT_RUN_LABEL:
        return base_name
    return f"{base_name}{RUN_SUFFIX_PREFIX}{normalized}"
