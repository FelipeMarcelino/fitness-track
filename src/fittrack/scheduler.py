"""Scheduler (spec 3.1): periodic jobs, and later the proactive coach windows.

Same shape as the worker, and the same emptiness for now.
"""

from __future__ import annotations

from pathlib import Path

from fittrack.runtime import DEFAULT_INTERVAL_S, heartbeat_loop, run_until_signalled

HEARTBEAT = Path("/tmp/fittrack-scheduler.hb")


async def run(heartbeat: Path = HEARTBEAT, interval_s: float = DEFAULT_INTERVAL_S) -> None:
    await heartbeat_loop(heartbeat, interval_s)


def main() -> None:
    run_until_signalled(HEARTBEAT)


if __name__ == "__main__":
    main()
