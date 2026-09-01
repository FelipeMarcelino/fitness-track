"""Worker (spec 3.1): ARQ consumer for flush_check and future process_batch.

Workers are stateless by contract (CLAUDE.md, invariant 5). State lives in
Postgres, Redis and Qdrant; any worker processes any job.

The heartbeat proves liveness to the container runtime. The ARQ functions
are the actual work: flush_check drains tenant buffers after the debounce
window closes (S02-T04), and process_batch will drive the LangGraph graph
(S02-T05, Sprint 03).
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any

from arq import ArqRedis

from fittrack.runtime import DEFAULT_INTERVAL_S, heartbeat_loop, run_until_signalled
from fittrack.services.debounce import flush_check as _flush_check
from fittrack.startup import startup

HEARTBEAT = Path("/tmp/fittrack-worker.hb")


class ArqFlushScheduler:
    """``FlushScheduler`` backed by ARQ's job queue.

    Uses a stable ``_job_id`` of ``flush:{tenant_id}`` so that renewals
    replace instead of accumulating jobs (spec §17.1).
    """

    def __init__(self, pool: ArqRedis) -> None:
        self._pool = pool

    async def schedule_flush_check(self, *, tenant_id: int, delay_s: int) -> None:
        await self._pool.enqueue_job(
            "flush_check",
            tenant_id,
            _job_id=f"flush:{tenant_id}",
            _defer_by=timedelta(seconds=delay_s),
        )


async def flush_check(ctx: dict[str, Any], tenant_id: int) -> None:
    """ARQ entry point for the debounce gate (S02-T04).

    Registered with ``keep_result=0`` so the stable job id is immediately
    reusable after the function completes.
    """
    from fittrack.settings import get_settings

    settings = get_settings()
    redis: ArqRedis = ctx["redis"]
    scheduler = ArqFlushScheduler(redis)
    await _flush_check(
        tenant_id=tenant_id,
        redis=redis,  # type: ignore[arg-type]  # ArqRedis vs Protocol impedance
        scheduler=scheduler,
        debounce_window_s=settings.debounce_window_s,
    )


async def run(heartbeat: Path = HEARTBEAT, interval_s: float = DEFAULT_INTERVAL_S) -> None:
    await heartbeat_loop(heartbeat, interval_s)


def main() -> None:
    # Before the first beat: a service that reports healthy on an invalid
    # deployment is worse than one that never starts.
    startup("worker")
    run_until_signalled(HEARTBEAT)


if __name__ == "__main__":
    main()
