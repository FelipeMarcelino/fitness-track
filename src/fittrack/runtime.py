"""Shared loop for the two services that have no socket to answer on."""

from __future__ import annotations

import asyncio
import contextlib
import signal
from pathlib import Path

from fittrack.health import write_heartbeat

DEFAULT_INTERVAL_S = 5.0


async def heartbeat_loop(heartbeat: Path, interval_s: float = DEFAULT_INTERVAL_S) -> None:
    """Beat until cancelled.

    This is the whole of a worker for now (S01-T02 ships health and smoke only).
    The ARQ consumer replaces the body of the loop, not its shape: the process
    keeps no state between beats, so any replica can take over (CLAUDE.md,
    invariant 5).
    """
    while True:
        write_heartbeat(heartbeat)
        await asyncio.sleep(interval_s)


def run_until_signalled(heartbeat: Path, interval_s: float = DEFAULT_INTERVAL_S) -> None:
    """Entry point of a container: run the loop, stop cleanly on SIGTERM."""

    async def supervise() -> None:
        task = asyncio.create_task(heartbeat_loop(heartbeat, interval_s))
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, task.cancel)
        with contextlib.suppress(asyncio.CancelledError):
            await task

    asyncio.run(supervise())
