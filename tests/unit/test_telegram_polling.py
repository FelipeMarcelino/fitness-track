"""The getUpdates transport (spec 18.2; sprint S02-T08).

Polling is development-only, and these tests hold the three properties that
make it safe to run even there:

1. **The offset survives a restart.** It is persisted in Redis after a batch
   has been *delivered*, not fetched, so a crash between the two replays the
   batch — and the pipeline's own dedup is what makes that replay harmless.
2. **Nothing is confirmed before it is processed.** A dispatch that fails
   leaves the offset where it was; Telegram will hand the updates back.
3. **The calls ask for exactly what the pipeline understands.** The same
   `allowed_updates` list the bootstrap registers, `my_chat_member` included —
   without it the poller never sees a block, and `revoked_at` is unobservable.

No test opens a socket: the client takes an `httpx.AsyncClient`, and the tests
hand it a `MockTransport`.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from fittrack.channels.telegram.client import TelegramApiError, TelegramClient
from fittrack.channels.telegram.polling import (
    ALLOWED_UPDATES,
    POLL_TIMEOUT_S,
    RETRY_PAUSE_S,
    RedisOffsetStore,
    TelegramPoller,
)

TOKEN = "8100000000:AAH-this-is-not-a-real-bot-token-000000"


class MemoryOffsets:
    """The two Redis commands the poller needs, in memory."""

    def __init__(self, offset: int | None = None) -> None:
        self.saved: list[int] = []
        self._offset = offset

    async def load(self) -> int | None:
        return self._offset

    async def save(self, offset: int) -> None:
        self.saved.append(offset)
        self._offset = offset


class Scripted:
    """Answers `getUpdates` from a script, one batch per call.

    An entry that is an `Exception` is raised instead of answered, which is how
    a timeout or a 409 is rehearsed without a socket.
    """

    def __init__(self, batches: list[Any]) -> None:
        self.batches = list(batches)
        self.calls: list[dict[str, Any]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        body: dict[str, Any] = json.loads(request.content) if request.content else {}
        self.calls.append(body)
        if not self.batches:
            return httpx.Response(200, json={"ok": True, "result": []})
        batch = self.batches.pop(0)
        if isinstance(batch, Exception):
            raise batch
        if isinstance(batch, httpx.Response):
            return batch
        return httpx.Response(200, json={"ok": True, "result": batch})


def poller(script: Scripted, offsets: MemoryOffsets) -> TelegramPoller:
    http = httpx.AsyncClient(transport=httpx.MockTransport(script.handler))
    return TelegramPoller(
        client=TelegramClient(SecretStr(TOKEN), http=http),
        offsets=offsets,
    )


def update(update_id: int) -> dict[str, Any]:
    return {
        "update_id": update_id,
        "message": {"chat": {"id": 4242, "type": "private"}, "text": "oi"},
    }


async def test_the_first_call_asks_without_an_offset() -> None:
    script = Scripted([[]])
    offsets = MemoryOffsets()

    await poller(script, offsets).poll_once(lambda _: None)

    assert "offset" not in script.calls[0]


async def test_a_batch_confirms_one_past_its_last_update() -> None:
    script = Scripted([[update(100), update(101)], []])
    offsets = MemoryOffsets()

    await poller(script, offsets).poll_once(lambda _: None)
    await poller(script, offsets).poll_once(lambda _: None)

    assert offsets.saved == [102]
    assert script.calls[1]["offset"] == 102


async def test_an_empty_batch_saves_nothing_and_dispatches_nothing() -> None:
    script = Scripted([[], []])
    offsets = MemoryOffsets()
    delivered: list[dict[str, Any]] = []

    dispatched = await poller(script, offsets).poll_once(delivered.append)

    assert dispatched == 0
    assert delivered == []
    assert offsets.saved == []


async def test_a_failed_dispatch_leaves_the_offset_alone() -> None:
    script = Scripted([[update(500)]])
    offsets = MemoryOffsets()

    async def explode(payload: dict[str, Any]) -> None:
        raise RuntimeError("pipeline unavailable")

    with pytest.raises(RuntimeError, match="pipeline unavailable"):
        await poller(script, offsets).poll_once(explode)

    assert offsets.saved == []


async def test_the_offset_survives_a_restart() -> None:
    first = Scripted([[update(700)]])
    offsets = MemoryOffsets()
    await poller(first, offsets).poll_once(lambda _: None)
    assert offsets.saved == [701]

    # A new process, the same Redis: it resumes rather than replays.
    second = Scripted([[update(701)], []])
    await poller(second, offsets).poll_once(lambda _: None)

    assert second.calls[0]["offset"] == 701


async def test_the_calls_ask_for_what_the_pipeline_understands() -> None:
    script = Scripted([[]])
    offsets = MemoryOffsets()

    await poller(script, offsets).poll_once(lambda _: None)

    assert script.calls[0]["timeout"] == POLL_TIMEOUT_S
    assert script.calls[0]["allowed_updates"] == list(ALLOWED_UPDATES)
    assert "my_chat_member" in ALLOWED_UPDATES


async def test_a_webhook_conflict_is_reported_not_swallowed() -> None:
    """409 means the bootstrap never ran: hiding it would look like idle polling."""
    script = Scripted(
        [
            httpx.Response(
                409,
                json={
                    "ok": False,
                    "error_code": 409,
                    "description": (
                        "Conflict: terminated by other getUpdates request; "
                        "make sure that only one bot instance is running"
                    ),
                },
            )
        ]
    )
    offsets = MemoryOffsets()

    with pytest.raises(TelegramApiError) as caught:
        await poller(script, offsets).poll_once(lambda _: None)

    assert caught.value.status_code == 409


async def test_run_survives_a_transport_error_and_keeps_going(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A refused connection or a client timeout pauses and retries, not dies.

    The pause itself is faked out — a real `RETRY_PAUSE_S` here would make
    this test slow without proving anything the recorded call doesn't already
    prove — but its *use* is exactly the review finding this guards: a
    transport failure that returned instantly must not spin the loop at full
    speed (spec 18.2 review).
    """
    import asyncio

    script = Scripted([httpx.ReadTimeout("long poll expired"), [update(900)], []])
    offsets = MemoryOffsets()
    sleeps: list[float] = []

    real_sleep = asyncio.sleep

    async def fake_sleep(delay: float) -> None:
        # Records the requested pause without actually waiting it out — but
        # still yields once, for real: a fake with no `await` inside never
        # suspends, and the task under test would then never get a turn.
        sleeps.append(delay)
        await real_sleep(0)

    monkeypatch.setattr("fittrack.channels.telegram.polling.asyncio.sleep", fake_sleep)

    async def dispatch(payload: dict[str, Any]) -> None:
        return None

    task = asyncio.create_task(poller(script, offsets).run(dispatch))
    for _ in range(200):
        if offsets.saved:
            break
        await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert offsets.saved == [901]
    assert RETRY_PAUSE_S in sleeps


async def test_run_survives_a_dispatch_failure_and_keeps_going(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`dispatch` failing — a transient Redis or Postgres outage, an
    `UpdateInFlightError` — must pause and retry, the same as a transport
    failure, rather than silently end the task the ingress never awaits and
    so never learns died (spec 18.2 review).
    """
    import asyncio

    script = Scripted([[update(950)], [update(950)], []])
    offsets = MemoryOffsets()
    sleeps: list[float] = []
    attempts = 0

    real_sleep = asyncio.sleep

    async def fake_sleep(delay: float) -> None:
        # Records the requested pause without actually waiting it out — but
        # still yields once, for real: a fake with no `await` inside never
        # suspends, and the task under test would then never get a turn.
        sleeps.append(delay)
        await real_sleep(0)

    monkeypatch.setattr("fittrack.channels.telegram.polling.asyncio.sleep", fake_sleep)

    async def flaky_dispatch(payload: dict[str, Any]) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("redis unavailable")

    task = asyncio.create_task(poller(script, offsets).run(flaky_dispatch))
    for _ in range(200):
        if offsets.saved:
            break
        await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert attempts >= 2, "the loop must retry dispatch rather than end the task"
    # 950 replayed (never confirmed) until the second attempt confirmed it.
    assert offsets.saved == [951]
    assert RETRY_PAUSE_S in sleeps


def test_the_offset_store_names_its_key() -> None:
    """The key is bot-global by design: Telegram has one update stream per bot."""
    assert RedisOffsetStore.KEY == "telegram:polling:offset"


async def test_aclose_releases_the_pollers_own_connection_pool() -> None:
    """The poller's pool is separate from the adapter's (module docstring); it
    needs its own release, the same way `TelegramAdapter.aclose` releases its.
    """
    http = httpx.AsyncClient(transport=httpx.MockTransport(Scripted([]).handler))
    instance = TelegramPoller(
        client=TelegramClient(SecretStr(TOKEN), http=http),
        offsets=MemoryOffsets(),
    )

    await instance.aclose()

    assert http.is_closed
