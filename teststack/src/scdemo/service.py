"""Run a service's main coroutine with a clean SIGTERM/SIGINT shutdown."""

import asyncio
import signal
from collections.abc import Callable, Coroutine
from typing import Any

from scdemo.logs import log_event


async def _supervise(main: Callable[[], Coroutine[Any, Any, None]]) -> bool:
    """Run ``main``; return True if a signal stopped it."""
    loop = asyncio.get_running_loop()
    task = asyncio.ensure_future(main())
    signalled = False

    def stop() -> None:
        nonlocal signalled
        signalled = True
        task.cancel()

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop)
    try:
        await task
    except asyncio.CancelledError:
        if not signalled:
            raise  # cancelled from elsewhere, not by our handler
    finally:
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.remove_signal_handler(sig)
    return signalled


def run_service(main: Callable[[], Coroutine[Any, Any, None]]) -> None:
    """Run ``main`` until it returns or SIGTERM/SIGINT cancels it.

    On a signal, ``main`` is cancelled (its ``finally`` blocks run), then
    ``event=shutdown`` is logged and the process exits with code 0.
    """
    signalled = asyncio.run(_supervise(main))
    log_event("shutdown", signal=1 if signalled else 0)
