"""Wiring the real Telegram ingress from `Settings` (S02-T08).

`build_telegram_components` is pure composition — every collaborator is
handed in, nothing is opened — so it is tested here the way
`tests/unit/test_channel_contract.py` tests the registry's factories: by
checking what got wired to what, not by exercising real I/O.

`open_telegram_runtime`, the half that actually opens Postgres and Redis, is
deliberately not exercised here beyond its cheapest branch (no Telegram
channel enabled, which touches neither). The rest of it is
`tests/integration/test_telegram_pipeline_smoke.py`'s job, against real
services — a fake pool that never pings is not the same claim as a real one
that does.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

import pytest

from fittrack.ingress_wiring import build_telegram_components, open_telegram_runtime
from fittrack.services.webhook import (
    CachedIdentityResolver,
    RedisTenantBuffer,
    RedisUpdateDeduplicator,
    SqlIdentityRevoker,
    SqlRawMessageStore,
    TelegramWebhookIngress,
)
from fittrack.settings import Settings

CONFIG_DIR = Path(__file__).parents[2] / "config"


class Unused:
    """A collaborator the wiring only has to store, never call, in this test."""

    kind = "telegram"


def build_settings(**overrides: str) -> Settings:
    """Construct through the environment, the way the process will (spec: startup)."""
    base = {
        "DATABASE_URL": "postgresql+asyncpg://fittrack_runtime:p@postgres:5432/f?sslmode=verify-full",
        "REDIS_URL": "rediss://:p@redis:6379/0",
        "QDRANT_URL": "https://qdrant:6333",
        "FITTRACK_CHANNELS": "",
        "FITTRACK_ENCRYPTION_KEYS": json.dumps({"1": base64.b64encode(b"A" * 32).decode()}),
        "FITTRACK_ACTIVE_KEY_VERSION": "1",
        "FITTRACK_IDENTITY_PEPPER": "a-startup-pepper-of-sufficient-length",
        "FITTRACK_CONFIG_DIR": str(CONFIG_DIR),
    }
    base.update(overrides)
    with pytest.MonkeyPatch.context() as patch:
        for name, value in base.items():
            patch.setenv(name, value)
        return Settings(_env_file=None)


def test_build_telegram_components_wires_the_documented_collaborators() -> None:
    channel: Any = Unused()
    redis: Any = Unused()
    sessions: Any = Unused()
    cipher: Any = Unused()

    ingress = build_telegram_components(
        channel=channel,
        redis=redis,
        sessions=sessions,
        cipher=cipher,
        pepper=b"p" * 32,
        debounce_window_s=10,
    )

    assert isinstance(ingress, TelegramWebhookIngress)
    assert ingress._channel is channel
    assert isinstance(ingress._deduplicator, RedisUpdateDeduplicator)
    assert isinstance(ingress._identities, CachedIdentityResolver)
    assert isinstance(ingress._raw_messages, SqlRawMessageStore)
    assert isinstance(ingress._buffer, RedisTenantBuffer)
    # Without this, a `my_chat_member` block is persisted and never acted on
    # (spec 18.2 review) — `revoked_at` would only ever move reactively.
    assert isinstance(ingress._revoker, SqlIdentityRevoker)


def test_build_telegram_components_hashes_identities_with_the_given_pepper() -> None:
    """The cache's hash function has to be the one function `identity_hash`
    defines, salted with the pepper handed in — not a second implementation
    that could drift from it.
    """
    from fittrack.security.identity_hash import identity_hash

    channel: Any = Unused()
    redis: Any = Unused()
    sessions: Any = Unused()
    cipher: Any = Unused()
    ingress = build_telegram_components(
        channel=channel,
        redis=redis,
        sessions=sessions,
        cipher=cipher,
        pepper=b"p" * 32,
        debounce_window_s=10,
    )

    resolver: Any = ingress._identities
    assert resolver._hash_identity("telegram", "chat-1") == identity_hash(
        "telegram", "chat-1", b"p" * 32
    )


async def test_a_deployment_without_telegram_builds_no_runtime() -> None:
    """No channel enabled: `None`, before either DSN above is ever dialed.

    Both `DATABASE_URL` and `REDIS_URL` resolve nowhere real; reaching either
    would hang or raise well before returning, which is the cheap proof this
    test gets for free that the branch returns before connecting.
    """
    settings = build_settings(FITTRACK_CHANNELS="")

    runtime = await open_telegram_runtime(settings)

    assert runtime is None
