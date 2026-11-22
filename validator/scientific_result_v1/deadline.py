"""Absolute deadlines for DSPy's async HTTP transport behind synchronous Predict.

One daemon I/O loop avoids reusing cached async SDK clients across closed loops.
Cancellation stops the client request; a timed-out provider may still bill usage.
"""

import asyncio
from concurrent.futures import TimeoutError as FutureTimeout
import threading

_lock = threading.Lock()
_loop = None


def run_bounded(factory, timeout):
    global _loop
    with _lock:
        if _loop is None:
            _loop = asyncio.new_event_loop()
            threading.Thread(
                target=_loop.run_forever, name="claims-inference-io", daemon=True
            ).start()
        loop = _loop

    async def bounded():
        return await asyncio.wait_for(factory(), timeout=timeout)

    future = asyncio.run_coroutine_threadsafe(bounded(), loop)
    try:
        return future.result(timeout=timeout + 2)
    except FutureTimeout as exc:
        future.cancel()
        raise TimeoutError(
            f"Provider request exceeded its {timeout:g}s absolute deadline"
        ) from exc
