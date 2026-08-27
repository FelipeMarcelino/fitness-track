"""The three service entrypoints exist and start; they do nothing else yet.

S01-T02 puts `ingress`, `worker` and `scheduler` in compose with health probes
only. Anything resembling business behaviour here would be the fiction the
sprint explicitly forbids.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from starlette.testclient import TestClient

from fittrack.main import app
from fittrack.scheduler import run as run_scheduler
from fittrack.worker import run as run_worker


def test_the_ingress_answers_its_health_probe() -> None:
    with TestClient(app) as client:
        response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_the_ingress_exposes_nothing_else() -> None:
    paths = {route.path for route in app.routes if hasattr(route, "path")}
    assert paths <= {"/health", "/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"}


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
