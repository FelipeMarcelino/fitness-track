"""ARQ worker entrypoint.

Sprint 01 ships the shell only; the queue, the per-user lock and the graph
arrive in feat/worker-queue and feat/echo-graph.
"""

from __future__ import annotations

from typing import Any, ClassVar

from fittrack.settings import get_settings


async def startup(ctx: dict[str, Any]) -> None:
    ctx["settings"] = get_settings()


class WorkerSettings:
    """Referenced by `arq fittrack.worker.WorkerSettings` in docker-compose."""

    functions: ClassVar[list[Any]] = []
    on_startup = startup
