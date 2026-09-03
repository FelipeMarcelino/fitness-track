"""`getUpdates` long polling — development only (spec 18.2, S02-T08).

The 409 trap of 18.2 is what this module exists around: `getUpdates` and a
registered webhook are mutually exclusive, and two pollers at once are the same
conflict. So the poller is a transport and nothing more — it does not decide
what an update means, it does not touch the pipeline, and it never registers
anything. `scripts/bootstrap.py` reconciles the mode (`deleteWebhook` before
polling), and the deployment topology keeps one ingress replica in dev.

The offset is the only state, and it is confirmed *after* a batch has been
delivered, not after it has been fetched. A crash between the two replays the
batch on the next boot, which is exactly what the webhook path does on a
timeout — the pipeline's `seen:telegram:{update_id}` reservation is what makes
either replay harmless, so the transport needs no stronger guarantee.

Nothing here talks to Redis by itself: the offset store is injected, because
the adapter does not open connections of its own (S02-T02, principle 8).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from typing import Any, Protocol

from fittrack.channels.telegram.client import (
    TelegramApiError,
    TelegramClient,
    TelegramTransportError,
)

__all__ = [
    "ALLOWED_UPDATES",
    "POLL_TIMEOUT_S",
    "OffsetStore",
    "RedisOffsetStore",
    "TelegramPoller",
]

logger = logging.getLogger(__name__)

# The same list `setWebhook` registers, `my_chat_member` included: without it
# the bot never learns it was blocked, and `revoked_at` is unobservable
# (spec 18.2, sprint S02-T08).
ALLOWED_UPDATES = ("message", "callback_query", "message_reaction", "my_chat_member")

# Held under the client's 30s timeout on purpose: a long poll that returns an
# empty batch is the quiet case, and a client timeout shorter than the poll
# would turn it into an error loop.
POLL_TIMEOUT_S = 25

# The pause after any failed attempt — a 409, a refused connection, a
# malformed response, or `dispatch` itself raising. Every one of them retried
# at full speed is a busy loop with a log line; the fix for most of them is a
# human's (the webhook reconciliation, a database coming back), and the
# backoff only needs to keep the loop polite while it waits.
RETRY_PAUSE_S = 5.0


class OffsetStore(Protocol):
    """Where the confirmed offset lives — Redis, in every real deployment."""

    async def load(self) -> int | None:
        """The last confirmed offset, or None when nothing was confirmed yet."""

    async def save(self, offset: int) -> None:
        """Confirm every update before `offset` as delivered."""


class RedisOffsetStore:
    """The offset in Redis, so a poller restart resumes instead of replaying.

    The key is bot-global and carries no tenant: Telegram has one update
    stream per bot, and the update ids in it are global to the bot (spec 18.2).
    """

    KEY = "telegram:polling:offset"

    def __init__(self, redis: Any) -> None:
        self._redis = redis

    async def load(self) -> int | None:
        raw = await self._redis.get(self.KEY)
        if raw is None:
            return None
        if isinstance(raw, bytes):
            raw = raw.decode()
        return int(raw)

    async def save(self, offset: int) -> None:
        await self._redis.set(self.KEY, str(offset))


class TelegramPoller:
    """One bot's update stream, as fast asynchronous batches.

    The client is injected with its own connection pool: the adapter the
    registry built serves the webhook's outbound calls, and `getUpdates` holds
    a connection for the full long poll — sharing one pool would have the
    poller's held connection competing with every send.
    """

    def __init__(self, *, client: TelegramClient, offsets: OffsetStore) -> None:
        self._client = client
        self._offsets = offsets

    def __repr__(self) -> str:
        # The client holds the token; say nothing that a traceback could carry.
        return "TelegramPoller(...)"

    async def poll_once(self, dispatch: Any) -> int:
        """Fetch one batch, deliver it, and confirm it. Returns the batch size.

        `dispatch` is an awaitable-per-update callable. Its failure is the
        poller's failure: the offset stays put and Telegram replays the batch,
        which is the at-least-once contract the dedup reservation absorbs.
        """
        payload: dict[str, Any] = {
            "timeout": POLL_TIMEOUT_S,
            "allowed_updates": list(ALLOWED_UPDATES),
        }
        offset = await self._offsets.load()
        if offset is not None:
            payload["offset"] = offset

        batch = await self._client.call("getUpdates", payload)
        updates = self._updates(batch)
        for update in updates:
            await _deliver(dispatch, update)
        if updates:
            # `max`, not "the last one": Telegram documents ascending order but
            # nothing here should depend on it to not re-receive a whole batch.
            await self._offsets.save(max(int(u["update_id"]) for u in updates) + 1)
        return len(updates)

    async def aclose(self) -> None:
        """Release the poller's own connection pool.

        Separate from the adapter's by design (class docstring): the wiring
        that owns a poller closes it the same way it closes the adapter, via
        `getattr(obj, "aclose", None)` rather than a type it may not import.
        """
        await self._client.aclose()

    async def run(self, dispatch: Any) -> None:
        """Poll until cancelled. Nothing short of cancellation is fatal.

        A refused connection, a malformed response, a 409, or `dispatch`
        itself failing (a transient Redis or Postgres outage, an
        `UpdateInFlightError`) are all logged and paused rather than raised:
        this loop is the process's reason to exist, and the alternative — a
        background task that dies quietly on the first hiccup while the
        ingress keeps answering `/health` — turns a dev machine into a silent
        no-op until somebody notices the bot stopped answering. `poll_once`
        already leaves the offset unadvanced on any of these, so Telegram
        replays the update once the loop is polling again; the guarantee this
        method exists to keep is that it *is* polling again.

        Every branch pauses `RETRY_PAUSE_S` before the next attempt — a
        refused connection or a DNS failure returns instantly, and retrying
        it with no delay is a busy loop with a log line, not a poller. The
        long poll's own client-side timeout lands in the same branch, and
        pays the same pause on the rare occasion it fires; that is the cost
        of one bound serving every kind of transport failure instead of
        needing to tell them apart.

        One `sleep(0)` at the top of every iteration is a yield point, not a
        pause: a real `getUpdates` suspends for the whole long poll, but a
        transport that answers instantly (a fake in a test, a broken
        intermediary) would otherwise spin this loop without ever returning
        to the event loop — starving the very shutdown that is supposed to
        stop it.
        """
        while True:
            await asyncio.sleep(0)
            try:
                await self.poll_once(dispatch)
            except TelegramTransportError as error:
                logger.debug("poll interrupted, retrying", exc_info=error)
                await asyncio.sleep(RETRY_PAUSE_S)
            except TelegramApiError as error:
                logger.error(
                    "getUpdates refused; is a webhook still registered? (bootstrap reconciles)",
                    extra={"status_code": error.status_code},
                )
                await asyncio.sleep(RETRY_PAUSE_S)
            except Exception:
                # `dispatch` is `ingress.accept`: a bug in it, or the database
                # or Redis it depends on, must pause and retry exactly like a
                # transport failure — not end the task the ingress never
                # awaits and so never learns died (spec 18.2 review).
                logger.exception("dispatch failed; the update will be replayed")
                await asyncio.sleep(RETRY_PAUSE_S)

    # --- internals --------------------------------------------------------- #

    @staticmethod
    def _updates(batch: Any) -> list[Mapping[str, Any]]:
        """The updates Telegram answered with, as a list of JSON objects.

        An answer that is not a list, or a member that is not an object with a
        valid `update_id`, is a transport-shaped lie from an intermediary: the
        client already rejects non-JSON and non-object bodies, and this is the
        same refusal one level in. Skipping instead would silently drop
        updates; the safe failure is the one that gets retried.
        """
        if not isinstance(batch, list):
            raise TelegramTransportError("getUpdates answered with something that is not a list")
        updates: list[Mapping[str, Any]] = []
        for member in batch:
            if not isinstance(member, dict) or not isinstance(member.get("update_id"), int):
                raise TelegramTransportError("getUpdates answered with a malformed update")
            updates.append(member)
        return updates


async def _deliver(dispatch: Any, update: Mapping[str, Any]) -> None:
    """One update into the pipeline, awaited so the offset waits for it."""
    result = dispatch(update)
    if asyncio.iscoroutine(result):
        await result
