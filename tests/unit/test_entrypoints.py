"""The service entrypoints expose only their declared public surface.

S01-T02 put the health probes in place; S02-T03 adds Telegram's one webhook
route.  This test remains the allowlist so accidental administrative endpoints
do not become public.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from fittrack.main import app
from fittrack.scheduler import run as run_scheduler
from fittrack.worker import run as run_worker
from tests.unit.test_startup import environment


@pytest.fixture
def valid_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """The ingress validates its configuration before serving, so give it one."""
    import os

    for name in list(os.environ):
        if name.startswith(("FITTRACK_", "DATABASE_", "REDIS_", "QDRANT_", "TELEGRAM_", "WABA_")):
            monkeypatch.delenv(name, raising=False)
    for name, value in environment().items():
        monkeypatch.setenv(name, value)


def test_the_ingress_answers_its_health_probe(valid_environment: None) -> None:
    with TestClient(app) as client:
        response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_the_ingress_exposes_nothing_else() -> None:
    paths = {route.path for route in app.routes if hasattr(route, "path")}
    assert paths <= {
        "/health",
        "/webhook/telegram",
        "/openapi.json",
        "/docs",
        "/docs/oauth2-redirect",
        "/redoc",
    }


async def test_the_worker_beats_while_it_runs(tmp_path: Path) -> None:
    beat = tmp_path / "worker.hb"
    task = asyncio.create_task(run_worker(heartbeat=beat, interval_s=0.01))
    await asyncio.sleep(0.05)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert beat.exists()


async def test_the_scheduler_beats_while_it_runs(tmp_path: Path) -> None:
    beat = tmp_path / "scheduler.hb"
    task = asyncio.create_task(run_scheduler(heartbeat=beat, interval_s=0.01))
    await asyncio.sleep(0.05)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert beat.exists()
