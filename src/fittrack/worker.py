"""Worker (spec 3.1): ARQ consumer for flush_check and process_batch.

Workers are stateless by contract (CLAUDE.md, invariant 5). State lives in
Postgres, Redis and Qdrant; any worker processes any job.

The heartbeat proves liveness to the container runtime. The ARQ functions
are the actual work: flush_check drains tenant buffers after the debounce
window closes (S02-T04), and process_batch persists and marks batches done
(S02-T05). Sprint 03 adds the LangGraph graph execution inside
process_batch.

Both handlers schedule follow-up work, and ARQ decides how that is allowed to
happen.  Two rules run through this module:

* A job id is reusable from outside the job it names and nowhere else. ARQ
  refuses ``enqueue_job`` while the job key exists, and ``finish_job`` deletes
  that key *and* the queue entry when the running job returns — a job that
  re-enqueues itself under its own id is erased on the way out.
* ``Retry`` is the only way a handler asks to be run again; returning normally
  after a failure is ARQ's definition of success.

NOTE: The production entry point (main → run_until_signalled) does not yet
start the registered ARQ consumer. S02-T08 switches the container entry point
to ``arq fittrack.worker.WorkerSettings`` during bootstrap integration.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from typing import Any, ClassVar

from arq import ArqRedis, Retry, func
from arq.connections import RedisSettings
from arq.worker import Function
from sqlalchemy.ext.asyncio import AsyncEngine

from fittrack.db.engine import get_engine, session_factory
from fittrack.runtime import DEFAULT_INTERVAL_S, heartbeat_loop, run_until_signalled
from fittrack.security.crypto import ColumnCipher, Keyring
from fittrack.services.batch import (
    BatchEnqueuer,
    BatchLockContentionError,
    PostgresBatchStore,
)
from fittrack.services.batch import persist_batch as _persist_batch
from fittrack.services.batch import process_batch as _process_batch
from fittrack.services.debounce import LOCK_RETRY_DELAY_S, DrainResult
from fittrack.services.debounce import flush_check as _flush_check
from fittrack.settings import Settings, get_settings
from fittrack.startup import startup

logger = logging.getLogger(__name__)

HEARTBEAT = Path("/tmp/fittrack-worker.hb")

# Section 4.1: three attempts with exponential backoff. The cap keeps the last
# one at 4s, which is well inside the 90s job timeout of the default queue.
MAX_TRIES = 3
MAX_BACKOFF_EXPONENT = 2


def build_redis_settings(settings: Settings | None = None) -> RedisSettings:
    """Translate the validated Redis URL and trust root into ARQ settings."""
    config = settings or get_settings()
    redis = RedisSettings.from_dsn(config.redis_url.get_secret_value())
    return replace(
        redis,
        ssl_ca_certs=config.fittrack_tls_ca_file,
        ssl_check_hostname=True,
    )


def _backoff_s(ctx: dict[str, Any]) -> int:
    """Exponential backoff for the current attempt (§4.1): 1s, 2s, 4s."""
    job_try = ctx.get("job_try", 1)
    attempt: int = job_try if isinstance(job_try, int) and not isinstance(job_try, bool) else 1
    return int(2 ** min(max(attempt - 1, 0), MAX_BACKOFF_EXPONENT))


class ArqFlushScheduler:
    """``FlushScheduler`` backed by ARQ's job queue.

    The ingress schedules under the stable id ``flush:{tenant_id}``, so a
    burst of messages costs one check instead of one per message (§17.1).

    The worker cannot reuse that id, because it schedules the next check from
    inside the job the id names (see the module docstring). It passes
    ``chained=True`` and the check goes in under a fresh id. The duplicate
    that an ingress renewal may add alongside it is harmless: a flush check
    that arrives second finds the debounce still ticking or an empty buffer,
    and returns without touching anything.
    """

    def __init__(self, pool: ArqRedis, *, chained: bool = False) -> None:
        self._pool = pool
        self._chained = chained

    async def schedule_flush_check(self, *, tenant_id: int, delay_s: int) -> None:
        await self._pool.enqueue_job(
            "flush_check",
            tenant_id,
            _job_id=None if self._chained else f"flush:{tenant_id}",
            _defer_by=timedelta(seconds=delay_s),
        )


class ArqBatchEnqueuer:
    """``BatchEnqueuer`` backed by ARQ's job queue.

    ``flush_check`` enqueues under the stable id ``batch:{batch_id}`` so that
    a retried flush, which finds and reuses the row it already persisted,
    cannot put a second job on the queue for it.

    ``defer_process_batch`` is the lock contention path of §17.3, and it runs
    inside the job that id names — so the deferral takes a fresh id. Two jobs
    for one batch stay safe: ``process_batch`` serialises on the tenant lock
    and only ever processes a ``pending`` row.
    """

    def __init__(self, pool: ArqRedis) -> None:
        self._pool = pool

    async def enqueue_process_batch(self, *, tenant_id: int, batch_id: int) -> None:
        await self._pool.enqueue_job(
            "process_batch",
            tenant_id,
            batch_id,
            _job_id=f"batch:{batch_id}",
        )

    async def defer_process_batch(self, *, tenant_id: int, batch_id: int, delay_s: int) -> None:
        await self._pool.enqueue_job(
            "process_batch",
            tenant_id,
            batch_id,
            _defer_by=timedelta(seconds=delay_s),
        )


async def worker_startup(ctx: dict[str, Any]) -> None:
    """Validate configuration and inject durable ARQ worker dependencies."""
    settings, _ = startup("worker")
    engine = get_engine(settings)
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

    ``drain:{tenant_id}`` is kept until both of those succeed (§17.3), which
    is only half of the recovery: the drain that survives a database outage
    is processed by the *next* check, and ARQ schedules one only for a job
    that raises ``Retry``. So a failure is translated into one instead of
    escaping as a plain job failure.

    Registered with ``keep_result=0`` so the stable job id is immediately
    reusable after the function completes.
    """
    settings = get_settings()
    redis: ArqRedis = ctx["redis"]
    scheduler = ArqFlushScheduler(redis, chained=True)
    cipher = ColumnCipher(Keyring.from_settings(settings))
    store = ctx["batch_store"]
    enqueuer: BatchEnqueuer = ArqBatchEnqueuer(redis)

    async def persist_and_enqueue(result: DrainResult) -> None:
        batch_id = await _persist_batch(
            drain=result,
            tenant_id=tenant_id,
            cipher=cipher,
            store=store,
        )
        await enqueuer.enqueue_process_batch(tenant_id=tenant_id, batch_id=batch_id)

    try:
        await _flush_check(
            tenant_id=tenant_id,
            redis=redis,  # type: ignore[arg-type]  # ArqRedis vs Protocol impedance
            scheduler=scheduler,
            debounce_window_s=settings.debounce_window_s,
            drain_handler=persist_and_enqueue,
        )
    except Exception as error:
        raise Retry(defer=_backoff_s(ctx)) from error


async def process_batch(ctx: dict[str, Any], tenant_id: int, batch_id: int) -> None:
    """ARQ entry point for batch processing (S02-T05).

    Acquires the per-tenant lock and marks the batch ``done``.
    Sprint 03 adds the LangGraph graph execution inside this handler.

    A busy ``lock:{tenant_id}`` is the expected case of §17.3, not a failure:
    the batch is re-enqueued with a five second delay and the job returns.
    Spending an attempt on it would let a graph run that holds the lock for
    its full TTL exhaust the three tries of §4.1 without the batch ever being
    processed, leaving the row ``pending`` with nothing left to pick it up.
    Real failures do consume the budget, with exponential backoff — and a
    deferral that never reaches Redis is one of them, so the enqueue sits
    inside the retried block rather than beside it.
    """
    redis: ArqRedis = ctx["redis"]
    store = ctx["batch_store"]
    try:
        try:
            await _process_batch(
                tenant_id=tenant_id,
                batch_id=batch_id,
                redis=redis,  # type: ignore[arg-type]  # ArqRedis vs Protocol impedance
                store=store,
            )
        except BatchLockContentionError:
            await ArqBatchEnqueuer(redis).defer_process_batch(
                tenant_id=tenant_id,
                batch_id=batch_id,
                delay_s=LOCK_RETRY_DELAY_S,
            )
            logger.info(
                "tenant lock busy, batch deferred",
                extra={
                    "tenant_id": tenant_id,
                    "batch_id": batch_id,
                    "delay_s": LOCK_RETRY_DELAY_S,
                },
            )
    except Exception as error:
        raise Retry(defer=_backoff_s(ctx)) from error


def _worker_settings() -> type:
    """Assemble the ARQ settings class, once the environment is available.

    ARQ reads ``redis_settings`` out of the class ``__dict__``
    (``arq.worker.get_kwargs``), so it has to be a plain class attribute — and
    building it requires the validated ``REDIS_URL``. In a class body that
    would make *importing* this module read the environment, which no other
    module here does and which breaks every context that has none, test
    collection included. Access defers it instead: ``import_string`` resolves
    ``arq fittrack.worker.WorkerSettings`` with ``getattr`` on the module, and
    the module ``__getattr__`` below builds the class then.
    """

    class WorkerSettings:
        """ARQ registration for the S02-T04/T05 default queue handlers."""

        functions: ClassVar[list[Function]] = [
            func(flush_check, keep_result=0, max_tries=MAX_TRIES),
            func(process_batch, keep_result=0, max_tries=MAX_TRIES),
        ]
        redis_settings: ClassVar[RedisSettings] = build_redis_settings()
        queue_name = "arq:queue"
        max_jobs = 10
        job_timeout = 90
        on_startup = worker_startup
        on_shutdown = worker_shutdown

    return WorkerSettings


def __getattr__(name: str) -> Any:
    """Build ``WorkerSettings`` on access (see :func:`_worker_settings`).

    Rebuilt per access rather than cached: it is a handful of attributes, and
    a cache would freeze the configuration of whoever touched it first.
    """
    if name == "WorkerSettings":
        return _worker_settings()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


async def run(heartbeat: Path = HEARTBEAT, interval_s: float = DEFAULT_INTERVAL_S) -> None:
    await heartbeat_loop(heartbeat, interval_s)


def main() -> None:
    # Before the first beat: a service that reports healthy on an invalid
    # deployment is worse than one that never starts.
    startup("worker")
    run_until_signalled(HEARTBEAT)


if __name__ == "__main__":
    main()
