from __future__ import annotations

from typing import Any

from .tasks import PROTOCOL_VERSION, SCHEMA_VERSION

try:
    from bittensor import Synapse
except ImportError:  # pragma: no cover - used only when the SDK is not installed.

    class Synapse:  # type: ignore[no-redef]
        """Fallback base class so local imports can fail with clear runtime errors."""

        def __init__(self, **kwargs: Any) -> None:
            for key, value in kwargs.items():
                setattr(self, key, value)


class ClaimExtractionSynapse(Synapse):
    """Request and response envelope for Claims extraction tasks."""

    protocol_version: str = PROTOCOL_VERSION
    schema_version: str = SCHEMA_VERSION
    task_id: str = ""
    paper_id: str = ""
    paper_url: str = ""
    source_sha256: str = ""
    artifact: dict[str, Any] | None = None
    extraction: dict[str, Any] | None = None
    miner_version: str = "agent_v1"
    error: str = ""
