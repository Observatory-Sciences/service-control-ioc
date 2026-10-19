"""A throwaway service for testing the harness and run_service.

Run as ``python -m _probe_service`` (the tests put this directory on PYTHONPATH).
``SCDEMO_PROBE_MODE``: ``run`` (default) idles until SIGTERM, ``exit3`` exits 3, ``burst`` logs 500 lines on SIGTERM.
"""

import asyncio
import os
import sys

from scdemo.logs import log_event, setup_logging
from scdemo.service import run_service


async def main() -> None:
    log_event(
        "starting",
        hw_host=os.environ["SCDEMO_HW_HOST"],
        base=os.environ["SCDEMO_PORT_BASE"],
    )
    log_event("ready", note="two words")
    if os.environ.get("SCDEMO_PROBE_MODE") == "exit3":
        sys.exit(3)
    if os.environ.get("SCDEMO_PROBE_MODE") == "burst":
        try:
            await asyncio.Event().wait()
        finally:
            for i in range(500):
                log_event("burst", i=i)
    await asyncio.Event().wait()


if __name__ == "__main__":
    setup_logging("probe")
    run_service(main)
