"""ARQ worker entrypoint.

Sprint 01 ships the shell only; the queue, the per-user lock and the graph
arrive in feat/worker-queue and feat/echo-graph.
"""

from __future__ import annotations

from typing import Any, ClassVar

from arq.connections import RedisSettings

from fittrack.settings import get_settings


class _LazyRedisSettings:
    """Resolves on attribute access rather than at import.

    ARQ reads WorkerSettings.redis_settings when the worker boots, which is
    where the environment exists. Building it at import time would make simply
    importing this module require a full environment, so a unit test that never
    touches Redis would still need every credential.
    """

    def __get__(self, obj: object, objtype: type | None = None) -> RedisSettings:
        return RedisSettings.from_dsn(get_settings().redis_url.get_secret_value())


async def startup(ctx: dict[str, Any]) -> None:
    ctx["settings"] = get_settings()


class WorkerSettings:
    """Referenced by `arq fittrack.worker.WorkerSettings` in docker-compose.

    Without redis_settings, ARQ defaults to localhost -- which inside compose is
    the worker container itself. The worker would never reach the redis service
    and would sit in a restart loop, reporting a connection error that reads as
    "Redis is down" when Redis is fine.
    """

    functions: ClassVar[list[Any]] = []
    redis_settings = _LazyRedisSettings()
    on_startup = startup
