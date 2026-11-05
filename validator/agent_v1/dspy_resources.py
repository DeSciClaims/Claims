"""Release DSPy resources in the thread that performed the model request."""

from __future__ import annotations

import logging
import sys
from typing import Any


LOGGER = logging.getLogger(__name__)


def close_dspy_lm(lm: Any, *, dspy_module: Any = None) -> None:
    """Close model clients and this thread's disk-cache connections.

    DSPy's cache is global, but diskcache stores SQLite connections in thread
    locals. Closing it in the controller thread cannot release judging threads'
    connections. Its public ``close`` leaves entries on disk and other threads'
    connections intact; later requests reconnect lazily.
    """
    try:
        for target in (lm, getattr(lm, "client", None)):
            close = getattr(target, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    continue
    finally:
        module = dspy_module if dspy_module is not None else sys.modules.get("dspy")
        disk_cache = getattr(getattr(module, "cache", None), "disk_cache", None)
        close = getattr(disk_cache, "close", None)
        if callable(close):
            try:
                close()
            except Exception as exc:
                # Cleanup must not hide a completed result or the model error.
                LOGGER.warning("DSPy disk-cache cleanup failed (%s)", type(exc).__name__)
