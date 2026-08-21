"""ARQ worker: drains bursts and processes them (§17.2).

Sprint 01 ends at the batch. feat/echo-graph replaces `_handle` with the graph.
"""

from __future__ import annotations

import base64
import logging
from typing import Any, ClassVar

import redis.asyncio as aioredis
from arq.connections import RedisSettings
from sqlalchemy.ext.asyncio import create_async_engine

from fittrack.crypto.aesgcm import Encryptor, KeyRing
from fittrack.services.batch import Batch, BatchStore
from fittrack.services.debounce import BurstBuffer
from fittrack.services.pipeline import BUSY_RETRY_SECONDS, Pipeline
from fittrack.settings import get_settings

log = logging.getLogger(__name__)


class _LazyRedisSettings:
    """Resolves on attribute access rather than at import.

    ARQ reads WorkerSettings.redis_settings when the worker boots, which is
    where the environment exists. Building it at import time would make simply
    importing this module require a full environment.
    """

    def __get__(self, obj: object, objtype: type | None = None) -> RedisSettings:
        return RedisSettings.from_dsn(get_settings().redis_url.get_secret_value())


async def _handle(batch: Batch) -> None:
    """Placeholder for the graph.

    Logging rather than passing silently: a batch that reaches here and does
    nothing should be visible while the graph is still missing.
    """
    log.info("batch %s ready for the graph: %r", batch.id, batch.combined_text[:120])


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


async def reclaim_orphans(ctx: dict[str, Any]) -> None:
    """Returns batches stranded by a worker that died mid-drain (§17.3)."""
    reclaimed = await ctx["buffer"].reclaim_orphans()
    if reclaimed:
        log.warning("reclaimed %d orphaned batches", reclaimed)


async def startup(ctx: dict[str, Any]) -> None:
    settings = get_settings()
    key = base64.b64decode(settings.fittrack_encryption_key.get_secret_value())

    redis: aioredis.Redis = aioredis.from_url(  # type: ignore[no-untyped-call]
        settings.redis_url.get_secret_value(),
        decode_responses=True,
    )
    buffer = BurstBuffer(redis, window_seconds=settings.debounce_window_s)
    batches = BatchStore(create_async_engine(settings.database_url.get_secret_value()))

    ctx["settings"] = settings
    ctx["redis"] = redis
    ctx["buffer"] = buffer
    ctx["encryptor"] = Encryptor(KeyRing({1: key}, current_version=1))
    ctx["pipeline"] = Pipeline(redis, buffer, batches, _handle)
    ctx["redis_queue"] = ctx.get("redis")


async def shutdown(ctx: dict[str, Any]) -> None:
    redis = ctx.get("redis")
    if redis is not None:
        await redis.aclose()


class WorkerSettings:
    """Referenced by `arq fittrack.worker.WorkerSettings` in docker-compose.

    Without redis_settings, ARQ defaults to localhost -- which inside compose is
    the worker container itself. The worker would sit in a restart loop
    reporting a connection error that reads as "Redis is down" when Redis is
    fine.
    """

    functions: ClassVar[list[Any]] = [flush_user]
    cron_jobs: ClassVar[list[Any]] = []
    redis_settings = _LazyRedisSettings()
    on_startup = startup
    on_shutdown = shutdown
