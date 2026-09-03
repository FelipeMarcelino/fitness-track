"""Update → buffer → batch `done`, with nothing faked (S02-T08, doc/sprints/02).

This is the claim the sprint's exit criterion makes about the whole pipeline,
not about any one piece of it: a Telegram update handed to the real,
fully-wired ingress ends up as a `processing_batch` row marked `done`, through
real Postgres and real Redis.

`flush_check` and `process_batch` are called directly rather than through
ARQ's queue: the point here is the pipeline's correctness end to end, and
ARQ's own wiring (job ids, retries, the worker's `ctx`) is already
`tests/integration/test_process_batch.py`'s job. Calling the same production
functions the worker calls, just not through the queue, is what keeps this a
smoke test of the pipeline instead of a second copy of that suite.
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

from fittrack.db.engine import session_factory
from fittrack.ingress_wiring import open_telegram_runtime
from fittrack.security.crypto import ColumnCipher, Keyring
from fittrack.security.identity_hash import identity_hash
from fittrack.services.batch import PostgresBatchStore, persist_batch, process_batch
from fittrack.services.debounce import flush_check
from fittrack.settings import Settings
from fittrack.worker import ArqFlushScheduler
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

        # The debounce window above is 1s; outlast it so `flush_check` drains
        # instead of re-enqueuing (spec 17.1).
        await asyncio.sleep(1.2)

        drain = await flush_check(
            tenant_id=tenant_id,
            redis=runtime.redis,
            scheduler=ArqFlushScheduler(runtime.redis, chained=True),
            debounce_window_s=settings.debounce_window_s,
        )
        assert drain is not None, "the buffer must have held the message accept() appended"
        assert len(drain.items) == 1

        cipher = ColumnCipher(Keyring.from_settings(settings))
        store = PostgresBatchStore(session_factory(runtime.engine))
        batch_id = await persist_batch(drain=drain, tenant_id=tenant_id, cipher=cipher, store=store)
        assert batch_id is not None

        await process_batch(
            tenant_id=tenant_id, batch_id=batch_id, redis=runtime.redis, store=store
        )

        row = await store.get(batch_id=batch_id, tenant_id=tenant_id)
        assert row is not None
        assert row.status == "done"
    finally:
        await runtime.aclose()
