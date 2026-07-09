from __future__ import annotations

from typing import Any

try:
    from bittensor import Synapse
except ImportError:  # pragma: no cover - used only when the SDK is not installed.

    class Synapse:  # type: ignore[no-redef]
        """Fallback base class so local imports can fail with clear runtime errors."""

        def __init__(self, **kwargs: Any) -> None:
            for key, value in kwargs.items():
                setattr(self, key, value)


class ClaimExtractionSynapse(Synapse):
    """Request and response envelope for v0 claim extraction."""

    task_id: str = ""
    paper_id: str = ""
    artifact: dict[str, Any] | None = None
    extraction: dict[str, Any] | None = None
    miner_version: str = "v0"
    error: str = ""
