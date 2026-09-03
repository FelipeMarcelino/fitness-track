"""Update → buffer → batch `done`, with nothing faked (S02-T08, doc/sprints/02).

This is the claim the sprint's exit criterion makes about the whole pipeline,
not about any one piece of it: a Telegram update handed to the real,
fully-wired ingress ends up as a `processing_batch` row marked `done`, through
the real ARQ queue the worker container consumes — real Postgres and real
Redis, and the same `flush_check`/`process_batch` job functions, `on_startup`
and `on_shutdown` the deployed worker registers (`fittrack.worker.WorkerSettings`).

Driven with `arq`'s own `Worker` in burst mode rather than by calling
`flush_check`/`process_batch` as plain functions: a version of this test that
called them directly passed even while the worker's container command ran
only a heartbeat loop and never started the ARQ consumer at all (the bug
S02-T08 review found) — updates sat in the Redis buffer forever in that
deployment, and a direct-call test could not have caught it. Going through
the queue is what makes this a smoke test of the *deployed* path.
"""

from __future__ import annotations

import asyncio
import base64
import json
import secrets
import time
from pathlib import Path

import asyncpg
import pytest
from arq.worker import Worker

from fittrack import worker as worker_module
from fittrack.ingress_wiring import open_telegram_runtime
from fittrack.security.identity_hash import identity_hash
from fittrack.services.batch import PostgresBatchStore
from fittrack.settings import Settings
from tests.conftest import HOST, verified_dsn

CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"
PEPPER = "a-smoke-test-pepper-of-sufficient-length-32b"


@pytest.fixture
def clean_channel_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    """A deployment built from scratch, not the worker container's own `.env`."""
    import os

    for name in list(os.environ):
        if name.startswith(("FITTRACK_", "DATABASE_", "REDIS_", "QDRANT_", "TELEGRAM_", "WABA_")):
            monkeypatch.delenv(name, raising=False)
    return monkeypatch


def build_settings(
    monkeypatch: pytest.MonkeyPatch,
    *,
    app_dsn: str,
    redis_password: str,
    ca_file: Path,
) -> Settings:
    """A real, webhook-mode deployment against the dev stack.

    Webhook, not polling: this proves `open_telegram_runtime`'s wiring and
    `ingress.accept`, neither of which needs a poller — and polling would mean
    a real `deleteWebhook` call to Telegram, which is out of scope for a
    pipeline smoke test.
    """
    values = {
        "DATABASE_URL": verified_dsn(app_dsn),
        "REDIS_URL": f"rediss://:{redis_password}@{HOST['redis']}:6379/0",
        "QDRANT_URL": "https://qdrant:6333",
        "FITTRACK_TLS_CA_FILE": str(ca_file),
        "FITTRACK_CHANNELS": "telegram",
        "FITTRACK_ENCRYPTION_KEYS": json.dumps({"1": base64.b64encode(b"K" * 32).decode()}),
        "FITTRACK_ACTIVE_KEY_VERSION": "1",
        "FITTRACK_IDENTITY_PEPPER": PEPPER,
        "FITTRACK_CONFIG_DIR": str(CONFIG_DIR),
        "TELEGRAM_BOT_TOKEN": "123456:smoke-test-token-not-a-real-bot",
        "TELEGRAM_MODE": "webhook",
        "TELEGRAM_WEBHOOK_SECRET": "s" * 43,
        "TELEGRAM_WEBHOOK_URL": "https://ingress.example.com/webhook/telegram",
        # Short on purpose: the test waits it out once, rather than the default
        # 10s, to prove the drain without turning this into a slow test.
        "DEBOUNCE_WINDOW_S": "1",
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    return Settings(_env_file=None)


def telegram_update(
    *, update_id: int, chat_id: int, message_id: int, text: str
) -> dict[str, object]:
    return {
        "update_id": update_id,
        "message": {
            "message_id": message_id,
            "date": int(time.time()),
            "chat": {"id": chat_id, "type": "private"},
            "text": text,
        },
    }


async def test_a_webhook_update_becomes_a_done_batch(
    app_dsn: str,
    redis_password: str,
    ca_file: Path,
    migrated: None,
    owner: asyncpg.Connection,
    clean_channel_env: pytest.MonkeyPatch,
) -> None:
    settings = build_settings(
        clean_channel_env, app_dsn=app_dsn, redis_password=redis_password, ca_file=ca_file
    )

    runtime = await open_telegram_runtime(settings)
    assert runtime is not None
    assert runtime.poller_task is None, "webhook mode starts no poller"

    try:
        chat_id = secrets.randbelow(900_000_000) + 100_000_000
        update = telegram_update(
            update_id=secrets.randbelow(1_000_000_000),
            chat_id=chat_id,
            message_id=1,
            text="supino reto com 10kg, 8 repeticoes",
        )

        await runtime.ingress.accept(update)

        digest = identity_hash("telegram", str(chat_id), PEPPER.encode())
        tenant_id = await owner.fetchval(
            "SELECT tenant_id FROM channel_identity WHERE channel = 'telegram' "
            "AND external_id_hash = $1",
            digest,
        )
        assert tenant_id is not None, "accept() must have bootstrapped a tenant and identity"

        raw_message_count = await owner.fetchval(
            "SELECT count(*) FROM raw_message WHERE tenant_id = $1", tenant_id
        )
        assert raw_message_count == 1

        # `accept()` scheduled a real `flush_check` job, deferred by the 1s
        # debounce window above; outlast it so the burst worker below finds it
        # ready rather than still in the future (spec 17.1).
        await asyncio.sleep(1.2)

        # The same functions, `on_startup` and `on_shutdown`
        # `fittrack.worker.WorkerSettings` registers — burst mode drains the
        # queue and returns, which is what makes this the worker container's
        # actual consumption path rather than a hand assembled substitute.
        # `flush_check`'s own job enqueues `process_batch` mid-run; burst mode
        # keeps polling until the queue is empty, so it catches that too.
        burst_worker = Worker(
            functions=[worker_module.flush_check, worker_module.process_batch],
            redis_pool=runtime.redis,
            on_startup=worker_module.worker_startup,
            on_shutdown=worker_module.worker_shutdown,
            burst=True,
            poll_delay=0.1,
            handle_signals=False,
        )
        try:
            await burst_worker.async_run()
            store = burst_worker.ctx["batch_store"]
            assert isinstance(store, PostgresBatchStore)

            batch_id = await owner.fetchval(
                "SELECT id FROM processing_batch WHERE tenant_id = $1 ORDER BY id DESC LIMIT 1",
                tenant_id,
            )
            assert batch_id is not None, "flush_check must have persisted a batch"

            row = await store.get(batch_id=batch_id, tenant_id=tenant_id)
            assert row is not None
            assert row.status == "done"
        finally:
            await burst_worker.close()
    finally:
        await runtime.aclose()
