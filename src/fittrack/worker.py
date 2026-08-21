"""ARQ worker: drains bursts, runs them through the graph, sends the reply (§17.2).

The three stages are deliberately separate jobs rather than one: a send that
fails should not re-run the graph, and a graph that fails should not re-drain
the buffer.
"""

from __future__ import annotations

import base64
import logging
from typing import Any, ClassVar, Final

import redis.asyncio as aioredis
from arq import cron
from arq.connections import RedisSettings
from sqlalchemy.ext.asyncio import create_async_engine

from fittrack.channels.whatsapp.client import WhatsAppChannel
from fittrack.crypto.aesgcm import Encryptor, KeyRing
from fittrack.graph.build import build_graph
from fittrack.graph.checkpoint import checkpointer
from fittrack.graph.runtime import GraphRunner
from fittrack.services.batch import BatchStore
from fittrack.services.debounce import BurstBuffer
from fittrack.services.dispatcher import Dispatcher
from fittrack.services.outbound import OutboundQueue
from fittrack.services.pipeline import BUSY_RETRY_SECONDS, Pipeline
from fittrack.settings import get_settings

log = logging.getLogger(__name__)

# Orphans are rare and not urgent, but they are invisible until swept.
RECLAIM_EVERY_MINUTES: Final = 5


class _LazyRedisSettings:
    """Resolves on attribute access rather than at import.

    ARQ reads WorkerSettings.redis_settings when the worker boots, which is
    where the environment exists. Building it at import time would make simply
    importing this module require a full environment.
    """

    def __get__(self, obj: object, objtype: type | None = None) -> RedisSettings:
        return RedisSettings.from_dsn(get_settings().redis_url.get_secret_value())


async def flush_user(ctx: dict[str, Any], bsuid: str) -> None:
    """Scheduled by ingress after the debounce window (§4).

    Reschedules instead of waiting when another worker holds the user's lock:
    blocking here would hold a worker slot for the length of someone else's
    LLM call.
    """
    pipeline: Pipeline = ctx["pipeline"]
    batch = await pipeline.flush(bsuid)
    if batch is None and await ctx["buffer"].is_ready(bsuid):
        await ctx["redis_queue"].enqueue_job("flush_user", bsuid, _defer_by=BUSY_RETRY_SECONDS)


async def deliver_outbound(ctx: dict[str, Any], tenant_id: int, bsuid: str) -> None:
    """Drains one tenant's outbound queue (§18.5).

    Per tenant rather than a global sweep: RLS is FORCEd per tenant (§19.1), so
    a cross-tenant poll would see nothing, and ordering is per reply anyway.
    A backoff reschedules this same job rather than waiting for a poller.
    """
    dispatcher: Dispatcher = ctx["dispatcher"]
    sent = await dispatcher.deliver(tenant_id, bsuid)
    if sent:
        log.info("delivered %d bubbles to tenant %s", sent, tenant_id)


async def reclaim_orphans(ctx: dict[str, Any]) -> None:
    """Returns batches stranded by a worker that died mid-drain (§17.3)."""
    reclaimed = await ctx["buffer"].reclaim_orphans()
    if reclaimed:
        log.warning("reclaimed %d orphaned batches", reclaimed)


async def startup(ctx: dict[str, Any]) -> None:
    settings = get_settings()
    key = base64.b64decode(settings.fittrack_encryption_key.get_secret_value())

    # ARQ puts its own pool in ctx["redis"] before calling this, and that is
    # the only client with enqueue_job. It decodes nothing, though, so the
    # buffer and the lock get their own text-mode client: reclaim_orphans
    # partitions scanned keys with a str separator and would fail on bytes.
    ctx["redis_queue"] = ctx["redis"]

    redis: aioredis.Redis = aioredis.from_url(  # type: ignore[no-untyped-call]
        settings.redis_url.get_secret_value(),
        decode_responses=True,
    )
    buffer = BurstBuffer(redis, window_seconds=settings.debounce_window_s)
    engine = create_async_engine(settings.database_url.get_secret_value())
    batches = BatchStore(engine)
    channel = WhatsAppChannel(
        settings.waba_phone_number_id,
        settings.waba_token.get_secret_value(),
    )

    ctx["settings"] = settings
    ctx["redis_text"] = redis
    ctx["buffer"] = buffer
    ctx["encryptor"] = Encryptor(KeyRing({1: key}, current_version=1))
    outbound = OutboundQueue(engine)
    ctx["dispatcher"] = Dispatcher(outbound, channel, ctx["redis_queue"])

    # The checkpointer's tables are created by the owner at deploy time, not
    # here: the app role cannot create tables (§19.1), and finding that out on
    # the first message is the wrong time.
    saver_cm = checkpointer(settings.database_url.get_secret_value())
    ctx["_saver_cm"] = saver_cm
    saver = await saver_cm.__aenter__()
    runner = GraphRunner(build_graph(checkpointer=saver), outbound, ctx["redis_queue"])
    ctx["graph_runner"] = runner
    ctx["pipeline"] = Pipeline(redis, buffer, batches, runner.handle)


async def shutdown(ctx: dict[str, Any]) -> None:
    saver_cm = ctx.get("_saver_cm")
    if saver_cm is not None:
        await saver_cm.__aexit__(None, None, None)
    # Only ours; ARQ closes its own pool.
    redis = ctx.get("redis_text")
    if redis is not None:
        await redis.aclose()


class WorkerSettings:
    """Referenced by `arq fittrack.worker.WorkerSettings` in docker-compose.

    Without redis_settings, ARQ defaults to localhost -- which inside compose is
    the worker container itself. The worker would sit in a restart loop
    reporting a connection error that reads as "Redis is down" when Redis is
    fine.
    """

    functions: ClassVar[list[Any]] = [flush_user, deliver_outbound, reclaim_orphans]
    # Unregistered, reclaim_orphans never runs, and a worker killed between the
    # RENAME and the batch insert strands that burst in its drain key forever
    # once the lease expires -- messages the user sent and the bot never sees.
    cron_jobs: ClassVar[list[Any]] = [
        cron(reclaim_orphans, minute=set(range(0, 60, RECLAIM_EVERY_MINUTES)), run_at_startup=True)
    ]
    redis_settings = _LazyRedisSettings()
    on_startup = startup
    on_shutdown = shutdown
