"""Worker (spec 3.1): ARQ consumer for flush_check and process_batch.

Workers are stateless by contract (CLAUDE.md, invariant 5). State lives in
Postgres, Redis and Qdrant; any worker processes any job.

The heartbeat proves liveness to the container runtime. The ARQ functions
are the actual work: flush_check drains tenant buffers after the debounce
window closes (S02-T04), and process_batch persists and marks batches done
(S02-T05). Sprint 03 adds the LangGraph graph execution inside
process_batch.

NOTE: The production entry point (main → run_until_signalled) does not yet
start the registered ARQ consumer. S02-T08 switches the container entry point
to ``arq fittrack.worker.WorkerSettings`` during bootstrap integration.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any, ClassVar

from arq import ArqRedis, Retry, func
from arq.worker import Function
from sqlalchemy.ext.asyncio import AsyncEngine

from fittrack.db.engine import get_engine, session_factory
from fittrack.runtime import DEFAULT_INTERVAL_S, heartbeat_loop, run_until_signalled
from fittrack.services.batch import BatchLockContentionError, PostgresBatchStore
from fittrack.services.batch import persist_batch as _persist_batch
from fittrack.services.batch import process_batch as _process_batch
from fittrack.services.debounce import LOCK_RETRY_DELAY_S, DrainResult
from fittrack.services.debounce import flush_check as _flush_check
from fittrack.startup import startup

HEARTBEAT = Path("/tmp/fittrack-worker.hb")


class ArqFlushScheduler:
    """``FlushScheduler`` backed by ARQ's job queue.

    Uses a stable ``_job_id`` of ``flush:{tenant_id}`` so that renewals
    from the ingress replace instead of accumulating jobs (spec §17.1).

    Before enqueueing, existing job/result keys are deleted so that
    re-enqueue from within the running handler succeeds — ARQ 0.26
    silently drops ``enqueue_job`` when the ``_job_id`` key already exists.
    """

    def __init__(self, pool: ArqRedis) -> None:
        self._pool = pool

    async def schedule_flush_check(self, *, tenant_id: int, delay_s: int) -> None:
        job_id = f"flush:{tenant_id}"
        # ARQ 0.26 blocks enqueue_job when the job or result key exists.
        # When called from inside the running handler, the current job key
        # blocks re-enqueue.  Deleting it allows the deferred replacement.
        # This is safe: the worker has already loaded the job payload.
        queue = self._pool.default_queue_name
        await self._pool.delete(f"{queue}:{job_id}", f"{queue}:result:{job_id}")
        await self._pool.enqueue_job(
            "flush_check",
            tenant_id,
            _job_id=job_id,
            _defer_by=timedelta(seconds=delay_s),
        )


class ArqBatchEnqueuer:
    """``BatchEnqueuer`` backed by ARQ's job queue.

    Uses a stable ``_job_id`` of ``batch:{batch_id}`` so that
    re-enqueueing the same batch (e.g. on retry) is idempotent.

    ``WorkerSettings`` registers the handler with ``max_tries=3``.
    """

    def __init__(self, pool: ArqRedis) -> None:
        self._pool = pool

    async def enqueue_process_batch(
        self, *, tenant_id: int, batch_id: int, delay_s: int = 0
    ) -> None:
        await self._pool.enqueue_job(
            "process_batch",
            tenant_id,
            batch_id,
            _job_id=f"batch:{batch_id}",
            _defer_by=timedelta(seconds=delay_s) if delay_s else None,
        )


async def worker_startup(ctx: dict[str, Any]) -> None:
    """Inject durable database dependencies into the ARQ worker context."""
    engine = get_engine()
    ctx["db_engine"] = engine
    ctx["batch_store"] = PostgresBatchStore(session_factory(engine))


async def worker_shutdown(ctx: dict[str, Any]) -> None:
    """Dispose the worker's database pool cleanly."""
    engine = ctx.pop("db_engine", None)
    ctx.pop("batch_store", None)
    if isinstance(engine, AsyncEngine):
        await engine.dispose()


async def flush_check(ctx: dict[str, Any], tenant_id: int) -> None:
    """ARQ entry point for the debounce gate (S02-T04).

    When the debounce window closes and a buffer is drained, persists
    the ``processing_batch`` with encrypted ``combined_text`` and
    enqueues the ``process_batch`` job (S02-T05).

    Registered with ``keep_result=0`` so the stable job id is immediately
    reusable after the function completes.
    """
    from fittrack.security.crypto import ColumnCipher, Keyring
    from fittrack.settings import get_settings

    settings = get_settings()
    redis: ArqRedis = ctx["redis"]
    scheduler = ArqFlushScheduler(redis)
    cipher = ColumnCipher(Keyring.from_settings(settings))
    store = ctx["batch_store"]
    enqueuer = ArqBatchEnqueuer(redis)

    async def persist_and_enqueue(result: DrainResult) -> None:
        batch_id = await _persist_batch(
            drain=result,
            tenant_id=tenant_id,
            cipher=cipher,
            store=store,
        )
        await enqueuer.enqueue_process_batch(tenant_id=tenant_id, batch_id=batch_id)

    await _flush_check(
        tenant_id=tenant_id,
        redis=redis,  # type: ignore[arg-type]  # ArqRedis vs Protocol impedance
        scheduler=scheduler,
        debounce_window_s=settings.debounce_window_s,
        drain_handler=persist_and_enqueue,
    )


async def process_batch(ctx: dict[str, Any], tenant_id: int, batch_id: int) -> None:
    """ARQ entry point for batch processing (S02-T05).

    Acquires the per-tenant lock and marks the batch ``done``.
    Sprint 03 adds the LangGraph graph execution inside this handler.

    Registered with ``max_tries=3`` and ``keep_result=0`` in
    ``WorkerSettings``. Lock contention is an explicit five-second ARQ retry.
    """
    redis: ArqRedis = ctx["redis"]
    store = ctx["batch_store"]
    try:
        await _process_batch(
            tenant_id=tenant_id,
            batch_id=batch_id,
            redis=redis,  # type: ignore[arg-type]  # ArqRedis vs Protocol impedance
            store=store,
        )
    except BatchLockContentionError as error:
        raise Retry(defer=LOCK_RETRY_DELAY_S) from error
    except Exception as error:
        job_try = ctx.get("job_try", 1)
        attempt = job_try if isinstance(job_try, int) and not isinstance(job_try, bool) else 1
        backoff_s = 2 ** min(max(attempt - 1, 0), 2)
        raise Retry(defer=backoff_s) from error


class WorkerSettings:
    """ARQ registration for the S02-T04/T05 default queue handlers."""

    functions: ClassVar[list[Function]] = [
        func(flush_check, keep_result=0),
        func(process_batch, keep_result=0, max_tries=3),
    ]
    queue_name = "arq:queue"
    max_jobs = 10
    job_timeout = 90
    on_startup = worker_startup
    on_shutdown = worker_shutdown


async def run(heartbeat: Path = HEARTBEAT, interval_s: float = DEFAULT_INTERVAL_S) -> None:
    await heartbeat_loop(heartbeat, interval_s)


def main() -> None:
    # Before the first beat: a service that reports healthy on an invalid
    # deployment is worse than one that never starts.
    startup("worker")
    run_until_signalled(HEARTBEAT)


if __name__ == "__main__":
    main()
