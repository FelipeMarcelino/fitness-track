"""The worker must reach the redis service, not localhost."""

from __future__ import annotations

import pytest

from fittrack.worker import WorkerSettings
from tests.unit.test_settings import REQUIRED


def test_redis_settings_come_from_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ARQ defaults to localhost. Inside compose that is the worker container
    itself, so the worker would restart-loop against a Redis that is fine."""
    for key, value in REQUIRED.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("REDIS_URL", "redis://redis:6379/3")

    from fittrack.settings import get_settings

    get_settings.cache_clear()
    settings = WorkerSettings.redis_settings

    assert settings.host == "redis"
    assert settings.port == 6379
    assert settings.database == 3
    get_settings.cache_clear()


def test_importing_the_module_does_not_require_an_environment() -> None:
    """Building redis_settings at import time would make a unit test that never
    touches Redis still need every credential."""
    import importlib

    module = importlib.import_module("fittrack.worker")
    assert module.WorkerSettings.functions, "the worker must register its jobs"


def test_the_orphan_reclaimer_is_scheduled() -> None:
    """Defining reclaim_orphans is not running it.

    Unregistered, a worker killed between the RENAME and the batch insert
    leaves that burst in its drain key forever once the lease expires: out of
    the buffer, absent from processing_batch, and swept by nothing.
    """
    from fittrack.worker import reclaim_orphans

    assert reclaim_orphans in WorkerSettings.functions
    scheduled = [job for job in WorkerSettings.cron_jobs if job.coroutine is reclaim_orphans]
    assert scheduled, "reclaim_orphans is defined but never scheduled"


def test_the_queue_client_is_the_one_that_can_enqueue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only ARQ's own pool has enqueue_job.

    A plain redis.asyncio.Redis passes every type check here and then raises
    AttributeError at the one moment it is needed -- when a user is already
    locked and the flush has to be rescheduled.
    """
    import asyncio

    from fittrack.worker import startup

    for key, value in REQUIRED.items():
        monkeypatch.setenv(key, value)

    from fittrack.settings import get_settings

    get_settings.cache_clear()

    class FakePool:
        """Stands in for ArqRedis: the only thing that matters is enqueue_job."""

        async def enqueue_job(self, _function: str, *_args: object, **_kwargs: object) -> None:
            return None

    ctx: dict[str, object] = {"redis": FakePool()}
    asyncio.run(startup(ctx))
    get_settings.cache_clear()

    assert hasattr(ctx["redis_queue"], "enqueue_job")
    assert ctx["redis_queue"] is ctx["redis"]


def test_the_delivery_job_is_registered() -> None:
    """The dispatcher reschedules itself by name after a backoff.

    Unregistered, that enqueue lands a job ARQ cannot run, and every retryable
    send failure becomes a permanent one.
    """
    from fittrack.worker import deliver_outbound

    assert deliver_outbound in WorkerSettings.functions
    assert deliver_outbound.__name__ == "deliver_outbound", (
        "the dispatcher enqueues this name literally"
    )
