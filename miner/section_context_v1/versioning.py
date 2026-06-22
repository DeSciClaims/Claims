from __future__ import annotations

import re
from pathlib import Path


DEFAULT_RUN_LABEL = "default"
RUN_SUFFIX_PREFIX = "__run_"


def normalize_run_label(value: str | None) -> str:
    raw = (value or "").strip()
    if not raw:
        return DEFAULT_RUN_LABEL
    normalized = re.sub(r"[^A-Za-z0-9._-]+", "-", raw).strip("-._")
    return normalized or DEFAULT_RUN_LABEL


def is_default_run_label(value: str | None) -> bool:
    return normalize_run_label(value) == DEFAULT_RUN_LABEL


def versioned_name(base_name: str, run_label: str | None) -> str:
    normalized = normalize_run_label(run_label)
    if normalized == DEFAULT_RUN_LABEL:
        return base_name
    return f"{base_name}{RUN_SUFFIX_PREFIX}{normalized}"


def parse_versioned_name(name: str) -> tuple[str, str]:
    if RUN_SUFFIX_PREFIX not in name:
        return name, DEFAULT_RUN_LABEL
    base_name, _, suffix = name.rpartition(RUN_SUFFIX_PREFIX)
    if not base_name or not suffix:
        return name, DEFAULT_RUN_LABEL
    return base_name, normalize_run_label(suffix)


def versioned_child_dir(parent: Path, child_name: str, run_label: str | None) -> Path:
    return parent / versioned_name(child_name, run_label)
