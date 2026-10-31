"""Host-owned memory admission limits for persistent paper execution slots."""

from pathlib import Path
from typing import Mapping

GIB = 1024**3
SCHEMA = "claims_worker_control_v2"
FIELDS = {"memory_per_job_bytes", "host_reserved_memory_bytes", "max_parallel_jobs"}


def validate_capacity_config(config: Mapping) -> None:
    limits = {
        "memory_per_job_bytes": (256 * 1024**2, 1024 * GIB),
        "host_reserved_memory_bytes": (GIB, 1024 * GIB),
        "max_parallel_jobs": (1, 64),
    }
    if any(
        type(config.get(k)) is not int or not lo <= config[k] <= hi
        for k, (lo, hi) in limits.items()
    ):
        raise ValueError("worker memory admission configuration is invalid")


def memory_info(path: Path = Path("/proc/meminfo")) -> tuple[int, int]:
    values = {}
    for line in path.read_text().splitlines():
        key, _, value = line.partition(":")
        if key in {"MemTotal", "MemAvailable"}:
            values[key] = int(value.strip().split()[0]) * 1024
    total, available = values["MemTotal"], values["MemAvailable"]
    if not 0 <= available <= total:
        raise ValueError("worker memory information is invalid")
    return total, available


def memory_slots(config: Mapping, total: int, available: int) -> tuple[int, int]:
    """Reserve the full hard limit per job, plus host headroom; never overcommit."""
    validate_capacity_config(config)
    size, reserve = config["memory_per_job_bytes"], config["host_reserved_memory_bytes"]
    slots = min(config["max_parallel_jobs"], max(0, (total - reserve) // size))
    admissions = min(slots, max(0, (available - reserve) // size))
    return slots, admissions
