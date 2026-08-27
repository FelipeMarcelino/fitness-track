"""Worker (spec 3.1): runs the LangGraph graph, once there is one.

Today it only proves it is alive. Workers are stateless by contract, so the
heartbeat lives in the container's own filesystem and means nothing outside it.
"""

from __future__ import annotations

from pathlib import Path

from fittrack.runtime import DEFAULT_INTERVAL_S, heartbeat_loop, run_until_signalled

HEARTBEAT = Path("/tmp/fittrack-worker.hb")


async def run(heartbeat: Path = HEARTBEAT, interval_s: float = DEFAULT_INTERVAL_S) -> None:
    await heartbeat_loop(heartbeat, interval_s)


def main() -> None:
    run_until_signalled(HEARTBEAT)


if __name__ == "__main__":
    main()
