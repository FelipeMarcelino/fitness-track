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

``main()`` runs the registered ARQ consumer via ``arq``'s own ``run_worker``
(S02-T08) — the container entry point (``python -m fittrack.worker``) needs no
change, since ``run_worker`` is what ``arq fittrack.worker.WorkerSettings``
calls internally too. The heartbeat rides along inside it: ``worker_startup``
starts ``heartbeat_loop`` as a background task and ``worker_shutdown`` cancels
it, so the container healthcheck keeps working under a process that spends
almost all of its time polling Redis rather than sleeping in a loop of its
own.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from typing import Any, ClassVar

import httpx
from arq import ArqRedis, Retry, cron, func
from arq.connections import RedisSettings
from arq.cron import CronJob
from arq.worker import Function, run_worker
from sqlalchemy.ext.asyncio import AsyncEngine

from fittrack.channels.registry import ChannelRegistry, ChannelRegistryError
from fittrack.config import Config, ConfigError
from fittrack.db.engine import get_engine, session_factory
from fittrack.runtime import DEFAULT_INTERVAL_S, heartbeat_loop
from fittrack.security.crypto import ColumnCipher, Keyring
from fittrack.services.batch import (
    BatchEnqueuer,
    BatchLockContentionError,
    PostgresBatchStore,
    VoiceResolver,
)
from fittrack.services.batch import persist_batch as _persist_batch
from fittrack.services.batch import process_batch as _process_batch
from fittrack.services.debounce import LOCK_RETRY_DELAY_S, DrainResult
from fittrack.services.debounce import flush_check as _flush_check
from fittrack.services.outbound import (
    OutboundService,
    PostgresOutboundQueueStore,
    RedisRateLimiter,
)
from fittrack.services.stt import (
    DEFAULT_RETRY_DIR,
    GroqTranscriber,
    SqlConsentGate,
    SqlTranscriptStore,
    SqlUsageLedger,
    VoiceIngestion,
    purge_stale_audio,
)
from fittrack.settings import ChannelKind, Settings, get_settings
from fittrack.startup import startup

logger = logging.getLogger(__name__)

HEARTBEAT = Path("/tmp/fittrack-worker.hb")

# Section 4.1: three attempts with exponential backoff. The cap keeps the last
# one at 4s, which is well inside the 90s job timeout of the default queue.
MAX_TRIES = 3
MAX_BACKOFF_EXPONENT = 2

# One job's budget. The transcription of S02-T07 runs inside `flush_check`, so
# this has to cover the download, the provider call and the persistence — a
# request still in flight when ARQ cancels the job never reaches the handler
# that keeps the recording for a retry. `SttConfig.timeout_s` is held below it
# by `tests/unit/test_stt.py`.
JOB_TIMEOUT = 90

# How often the retention sweep of §11.3 runs on its own, for the worker that
# keeps a failed recording and then receives no more voice.
AUDIO_SWEEP_MINUTES = frozenset({0, 30})

# What the voice step of one burst may spend, leaving the rest of `flush_check`
# — the drain read, the encryption, the insert and the enqueue — inside the job
# budget. ARQ kills a job at `job_timeout` and does *not* retry it
# (`TimeoutError` is not `CancelledError`, so its "will be run again" branch
# never fires), and the drain is then orphaned in Redis with nothing to
# re-drive it until the tenant sends another message. A burst of slow
# recordings must not be able to reach that.
VOICE_BUDGET_S = JOB_TIMEOUT - 20


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


# The channel whose voice notes this worker can fetch. Phase 1.0 is Telegram
# only (spec 24), and `download_media` takes a reference without saying which
# channel issued it — so the wiring names the channel once, here, and
# `VoiceIngestion` refuses an item from any other.
VOICE_CHANNEL: ChannelKind = "telegram"


def build_voice_ingestion(
    ctx: dict[str, Any],
    settings: Settings,
    config: Config,
) -> VoiceIngestion | None:
    """The transcription step of the drain, or ``None`` when it cannot run.

    Three things have to be true: `models.yaml` declares the `stt:` section,
    the transcription provider has a credential, and the channel that carries
    voice is enabled with an adapter that builds. Any of them missing is a
    deployment that cannot transcribe, and it says so once here — a voice item
    then reaches the batch with empty text and ``status='incomplete'`` rather
    than failing the whole drain (invariant 6).

    The reasons are enumerated rather than caught wholesale: those four are the
    states a deployment can legitimately be in, and anything else is a bug in
    this wiring, which should stop the worker instead of quietly turning voice
    off for a release.
    """
    try:
        stt = config.models.require_stt()
    except ConfigError as error:
        logger.warning("voice is disabled: %s", error)
        return None

    credential = getattr(settings, f"{stt.provider}_api_key", None)
    if credential is None:
        logger.warning(
            "voice is disabled: the transcription provider has no credential",
            extra={"provider": stt.provider},
        )
        return None

    if VOICE_CHANNEL not in settings.channels:
        logger.warning("voice is disabled: the channel that carries it is not enabled")
        return None
    try:
        channel = ChannelRegistry.from_config(settings).get(VOICE_CHANNEL)
    except ChannelRegistryError as error:
        logger.warning("voice is disabled: %s", error)
        return None

    sessions = session_factory(ctx["db_engine"])
    cipher = ColumnCipher(Keyring.from_settings(settings))
    # Kept so `worker_shutdown` can release the adapter's connection pool: the
    # registry is discarded here and the `Channel` protocol of 18.1 declares no
    # lifecycle method, so nobody else holds it.
    ctx["voice_channel"] = channel
    # What the half-hourly sweep needs, without re-reading the configuration.
    ctx["voice_retention_s"] = stt.retry_retention_hours * 3600
    # One pool for the worker, closed at shutdown. The adapter's own client is
    # separate and owned by the registry.
    http = httpx.AsyncClient(timeout=stt.timeout_s)
    ctx["stt_http"] = http
    return VoiceIngestion(
        channel=VOICE_CHANNEL,
        budget_s=VOICE_BUDGET_S,
        downloader=channel,
        transcriber=GroqTranscriber(http=http, api_key=credential, config=stt),
        consent=SqlConsentGate(sessions),
        transcripts=SqlTranscriptStore(sessions, cipher),
        # The cost line of §11.3. Accounting only: a ledger failure is logged
        # and never costs the user their transcription.
        usage=SqlUsageLedger(sessions),
        config=stt,
        prompt_dir=Path(settings.fittrack_config_dir) / "prompts",
        # The fixed replies of §11.3 leave through the single output path of
        # invariant 2, as ordinary queued responses (S02-T06). ARQ has put the
        # pool in `ctx` before `on_startup` runs, so the limiter is the shared
        # one every worker observes rather than a per-process semaphore.
        replies=OutboundService(
            store=PostgresOutboundQueueStore(sessions, cipher),
            rate_limiter=RedisRateLimiter(ctx["redis"]),
        ),
    )


async def worker_startup(ctx: dict[str, Any]) -> None:
    """Validate configuration and inject durable ARQ worker dependencies.

    An invalid deployment must not report healthy: `startup` runs before the
    heartbeat task does, so a bad environment fails the container before it
    ever beats — the same guarantee `run_until_signalled` gives the ingress
    and the scheduler.
    """
    settings, config = startup("worker")
    engine = get_engine(settings)
    ctx["db_engine"] = engine
    ctx["batch_store"] = PostgresBatchStore(session_factory(engine))
    ctx["voice"] = build_voice_ingestion(ctx, settings, config)
    # ARQ's own loop spends most of its time polling Redis, not sleeping in a
    # loop of its own — the heartbeat needs to run alongside it, inside the
    # same event loop `on_startup`/`on_shutdown` already run in.
    ctx["_heartbeat_task"] = asyncio.create_task(heartbeat_loop(HEARTBEAT))


async def sweep_voice_audio(ctx: dict[str, Any]) -> None:
    """Expire recordings past the retention window of §11.3.

    Registered as a cron job because the rule is a maximum, not a best effort:
    an ingestion-time sweep covers a worker that keeps receiving voice, and a
    replica that kept one failed recording and then went quiet would otherwise
    hold it for the life of the container.
    """
    retention_s = ctx.get("voice_retention_s")
    if not isinstance(retention_s, int):
        return  # voice is not wired in this deployment
    removed = purge_stale_audio(DEFAULT_RETRY_DIR, max_age_s=retention_s)
    if removed:
        logger.info("expired retained recordings", extra={"removed": removed})


async def worker_shutdown(ctx: dict[str, Any]) -> None:
    """Dispose the worker's database pool and HTTP clients cleanly."""
    heartbeat_task = ctx.pop("_heartbeat_task", None)
    if isinstance(heartbeat_task, asyncio.Task):
        heartbeat_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await heartbeat_task
    engine = ctx.pop("db_engine", None)
    ctx.pop("batch_store", None)
    ctx.pop("voice", None)
    ctx.pop("voice_retention_s", None)
    http = ctx.pop("stt_http", None)
    if isinstance(http, httpx.AsyncClient):
        await http.aclose()
    # The adapter owns the pool the registry built for it. `Channel` does not
    # declare `aclose` (spec 18.1), so this asks rather than assumes.
    channel = ctx.pop("voice_channel", None)
    close_channel = getattr(channel, "aclose", None)
    if callable(close_channel):
        await close_channel()
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

    voice: VoiceResolver | None = ctx.get("voice")

    async def persist_and_enqueue(result: DrainResult) -> None:
        batch_id = await _persist_batch(
            drain=result,
            tenant_id=tenant_id,
            cipher=cipher,
            store=store,
            voice=voice,
        )
        if batch_id is None:
            # Every item was answered on its own — an inaudible or over-long
            # voice note, say (§11.3). There is no batch to hand to the graph,
            # and the drain is acknowledged either way.
            return
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
        cron_jobs: ClassVar[list[CronJob]] = [
            cron(
                sweep_voice_audio,
                minute=set(AUDIO_SWEEP_MINUTES),
                run_at_startup=True,
                keep_result=0,
            )
        ]
        redis_settings: ClassVar[RedisSettings] = build_redis_settings()
        queue_name = "arq:queue"
        max_jobs = 10
        job_timeout = JOB_TIMEOUT
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
    # Before the first job: a service that reports healthy on an invalid
    # deployment is worse than one that never starts. `worker_startup` (ARQ's
    # `on_startup` hook, below) validates again once the consumer itself is
    # running — that second check is what actually gates the heartbeat and the
    # first job, this one is the fast fail before a Redis pool is even opened.
    startup("worker")
    run_worker(_worker_settings())


if __name__ == "__main__":
    main()
