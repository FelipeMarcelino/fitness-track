"""Wires the real Telegram ingress — and, in dev, its poller — from `Settings`.

Split in two on purpose:

- `build_telegram_components` is pure composition. Every collaborator is
  handed in and nothing is opened, which is what lets
  `tests/unit/test_ingress_wiring.py` exercise it the way
  `channels.registry`'s factories are exercised — by checking what got wired
  to what, with fakes standing in for Redis and the database.
- `open_telegram_runtime` is the half that actually connects: the registry's
  adapter, Postgres, and a Redis pool that pings on construction (`arq`'s
  `create_pool`, the same one `worker.py` uses). That needs real services, so
  it belongs to the ingress lifespan and to the smoke test of S02-T08, not to
  a unit test.

Neither function names a concrete Telegram type. `fittrack.channels.registry`
— `ChannelRegistry` and `build_telegram_poller` — is the one door into
`channels/` this module uses, exactly as
`tests/unit/test_channel_contract.py` requires of everything outside it.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass
from typing import Any

from arq import create_pool
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from fittrack.channels.registry import ChannelRegistry, build_telegram_poller
from fittrack.db.engine import get_engine, session_factory
from fittrack.security.crypto import ColumnCipher, Keyring
from fittrack.security.identity_hash import identity_hash
from fittrack.services.webhook import (
    CachedIdentityResolver,
    DatabaseIdentityResolver,
    RedisTenantBuffer,
    RedisUpdateDeduplicator,
    SqlIdentityRevoker,
    SqlRawMessageStore,
    TelegramWebhookIngress,
)
from fittrack.settings import ChannelKind, Settings
from fittrack.worker import ArqFlushScheduler, build_redis_settings

logger = logging.getLogger(__name__)

__all__ = ["TelegramRuntime", "build_telegram_components", "open_telegram_runtime"]

# Matches `CachedIdentityResolver`'s own default (services/webhook.py); named
# here because a wiring module that read the default by omission would break
# silently if that default ever changed for an unrelated reason.
IDENTITY_CACHE_TTL_S = 5 * 60


def build_telegram_components(
    *,
    channel: Any,
    redis: Any,
    sessions: async_sessionmaker[AsyncSession],
    cipher: ColumnCipher,
    pepper: bytes,
    debounce_window_s: int,
) -> TelegramWebhookIngress:
    """Compose the webhook ingress from already-open collaborators.

    One `redis` client plays three roles — the dedup reservation, the tenant
    buffer, and the identity cache — because all three are the same handful
    of Redis commands against the same pool; splitting it into three clients
    would only be three pools to open and close for no isolation gained.
    """
    scheduler = ArqFlushScheduler(redis)
    identities = CachedIdentityResolver(
        cache=redis,
        delegate=DatabaseIdentityResolver(sessions=sessions, cipher=cipher, pepper=pepper),
        hash_identity=lambda channel_kind, external_id: identity_hash(
            channel_kind, external_id, pepper
        ),
        ttl_s=IDENTITY_CACHE_TTL_S,
    )
    return TelegramWebhookIngress(
        channel=channel,
        deduplicator=RedisUpdateDeduplicator(redis),
        identities=identities,
        raw_messages=SqlRawMessageStore(sessions=sessions, cipher=cipher),
        buffer=RedisTenantBuffer(
            redis=redis, scheduler=scheduler, debounce_window_s=debounce_window_s
        ),
        # Without this, `revoked_at` only ever moves reactively, from a failed
        # send (services/outbound.py) — never from Telegram telling us
        # directly via `my_chat_member`, which is the whole reason that update
        # type is in `ALLOWED_UPDATES` (spec 18.2 review).
        revoker=SqlIdentityRevoker(sessions),
    )


@dataclass
class TelegramRuntime:
    """What the ingress lifespan owns for Telegram, wired and running.

    `poller_task` is `None` in webhook mode: there is nothing of the poller's
    own to cancel, because there is no poller (spec 18.2).
    """

    ingress: TelegramWebhookIngress
    channel: Any
    redis: Any
    engine: AsyncEngine
    poller: Any | None = None
    poller_task: asyncio.Task[None] | None = None

    async def aclose(self) -> None:
        """Reverse `open_telegram_runtime`, in the order that avoids leaks.

        The poller first: cancelling it stops the one thing still capable of
        calling into the ingress, before anything the ingress depends on is
        torn down underneath it.
        """
        if self.poller_task is not None:
            self.poller_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.poller_task
        for owner in (self.poller, self.channel):
            close = getattr(owner, "aclose", None)
            if callable(close):
                await close()
        await self.redis.aclose()
        await self.engine.dispose()


async def open_telegram_runtime(settings: Settings) -> TelegramRuntime | None:
    """Connect Telegram's ingress (and, in dev, its poller) for this process.

    `None` when Telegram is not an enabled channel: the ingress still answers
    `/health`, `/webhook/telegram` still exists and refuses with 503
    (`fittrack.main`), and nothing here opens a socket to decide that.
    """
    telegram: ChannelKind = "telegram"
    if telegram not in settings.channels:
        return None

    registry = ChannelRegistry.from_config(settings)
    channel = registry.get(telegram)

    engine = get_engine(settings)
    sessions = session_factory(engine)
    cipher = ColumnCipher(Keyring.from_settings(settings))
    pepper = settings.fittrack_identity_pepper.get_secret_value().encode()

    # Pings on construction (spec 18.2's "fail before serving"): a Redis this
    # process cannot reach must stop the ingress from reporting healthy, the
    # same way an invalid `Settings` already does in `fittrack.startup`.
    redis = await create_pool(build_redis_settings(settings))

    ingress = build_telegram_components(
        channel=channel,
        redis=redis,
        sessions=sessions,
        cipher=cipher,
        pepper=pepper,
        debounce_window_s=settings.debounce_window_s,
    )

    runtime = TelegramRuntime(ingress=ingress, channel=channel, redis=redis, engine=engine)

    if settings.telegram_mode == "polling":
        poller = build_telegram_poller(settings, redis=redis)
        runtime.poller = poller
        runtime.poller_task = asyncio.create_task(poller.run(ingress.accept))
        logger.info("telegram polling started")

    return runtime
