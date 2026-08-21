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
