"""The Telegram half of the error taxonomy (spec 18.4; sprint S02-T02).

`classify_error` is the only place that knows Telegram's vocabulary. The
outbound service reads the class and never a status code, which is what lets one
retry policy serve two channels with different ideas about failure.

The table in 18.4 is the specification and the parametrised test below is its
transcription — every row, including the two that are not really errors:
`message is not modified`, which is a successful no-op, and `message to react
not found`, which degrades a reaction into text rather than losing it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import httpx
import pytest
from pydantic import SecretStr

from fittrack.channels.base import ChannelIdentity, ErrorClass, OutboundBlock
from fittrack.channels.telegram.adapter import TelegramAdapter
from fittrack.channels.telegram.client import (
    TelegramApiError,
    TelegramClient,
    TelegramTransportError,
)

if TYPE_CHECKING:
    from collections.abc import Callable

TOKEN = "8100000000:AAH-this-is-not-a-real-bot-token-000000"
IDENTITY = ChannelIdentity(identity_id=1, tenant_id=7, channel="telegram", external_id="987654321")


def api_error(
    status: int, description: str, parameters: dict[str, Any] | None = None
) -> TelegramApiError:
    return TelegramApiError(
        method="sendMessage",
        status_code=status,
        description=description,
        parameters=parameters or {},
    )


def build(handler: Callable[[httpx.Request], httpx.Response]) -> TelegramAdapter:
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return TelegramAdapter(TelegramClient(SecretStr(TOKEN), http=http), webhook_secret=None)


ADAPTER = build(lambda request: httpx.Response(200, json={"ok": True, "result": True}))


# --------------------------------------------------------------------------- #
# The table of 18.4, row by row
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        pytest.param(
            api_error(403, "Forbidden: bot was blocked by the user"),
            ErrorClass.UNDELIVERABLE,
            id="403 blocked",
        ),
        pytest.param(
            api_error(403, "Forbidden: user is deactivated"),
            ErrorClass.UNDELIVERABLE,
            id="403 deactivated",
        ),
        pytest.param(
            api_error(400, "Bad Request: chat not found"),
            ErrorClass.UNDELIVERABLE,
            id="400 chat not found",
        ),
        pytest.param(
            api_error(400, "Bad Request: message to react not found"),
            ErrorClass.BUG,
            id="400 nothing to react to",
        ),
        pytest.param(
            api_error(400, "Bad Request: can't parse entities"),
            ErrorClass.BUG,
            id="400 malformed html",
        ),
        pytest.param(api_error(401, "Unauthorized"), ErrorClass.ACCOUNT, id="401"),
        pytest.param(api_error(500, "Internal Server Error"), ErrorClass.RETRY_BACKOFF, id="500"),
        pytest.param(api_error(502, "Bad Gateway"), ErrorClass.RETRY_BACKOFF, id="502"),
        pytest.param(api_error(503, "Service Unavailable"), ErrorClass.RETRY_BACKOFF, id="503"),
    ],
)
def test_every_row_of_the_telegram_table(error: Exception, expected: ErrorClass) -> None:
    assert ADAPTER.classify_error(error).error_class is expected


def test_a_rate_limit_keeps_the_number_telegram_sent() -> None:
    """The most practical difference from WhatsApp: an exact number, not a guess.

    18.4 says wait exactly `retry_after`. Carrying it on the verdict is what
    stops the outbound service from reaching into a channel's exception to find
    it.
    """
    verdict = ADAPTER.classify_error(
        api_error(429, "Too Many Requests: retry later", {"retry_after": 17})
    )
    assert verdict.error_class is ErrorClass.RETRY_AFTER
    assert verdict.retry_after == 17
    assert verdict.code == "429"


def test_a_rate_limit_without_a_number_falls_back_to_the_ladder() -> None:
    """`RETRY_AFTER` without seconds is a class the scheduler cannot use, and the
    type refuses to be built that way (S02-T01). A 429 that arrives without the
    parameter is a transient failure like any other.
    """
    verdict = ADAPTER.classify_error(api_error(429, "Too Many Requests"))
    assert verdict.error_class is ErrorClass.RETRY_BACKOFF
    assert verdict.retry_after is None


@pytest.mark.parametrize(
    "seconds",
    [
        pytest.param("17", id="a string"),
        pytest.param(-1, id="negative"),
        pytest.param(None, id="null"),
    ],
)
def test_a_retry_after_that_is_not_a_wait_is_not_believed(seconds: object) -> None:
    """A malformed parameter must not become a negative delay or a crash: both
    end as a retry that fires immediately, against a channel that just said no.
    """
    verdict = ADAPTER.classify_error(api_error(429, "Too Many Requests", {"retry_after": seconds}))
    assert verdict.error_class is ErrorClass.RETRY_BACKOFF


def test_a_timeout_is_transient() -> None:
    assert (
        ADAPTER.classify_error(TelegramTransportError("sendMessage timed out")).error_class
        is ErrorClass.RETRY_BACKOFF
    )


def test_something_that_is_not_a_channel_failure_is_our_bug() -> None:
    """A `TypeError` from our own code is not a reason to retry a send."""
    assert ADAPTER.classify_error(TypeError("nope")).error_class is ErrorClass.BUG


def test_the_verdict_carries_the_code_for_the_dead_letter_report() -> None:
    """The daily report breaks dead letters down by `(channel, error_code)`, and
    a class growing in one channel is a change in that API, not bad luck (18.4).
    """
    assert ADAPTER.classify_error(api_error(401, "Unauthorized")).code == "401"


def test_no_verdict_carries_the_token() -> None:
    verdict = ADAPTER.classify_error(api_error(401, "Unauthorized"))
    assert TOKEN not in repr(verdict)


# --------------------------------------------------------------------------- #
# The two rows that are not failures
# --------------------------------------------------------------------------- #


async def test_an_edit_that_changes_nothing_is_a_success() -> None:
    """`message is not modified` means the message is already what we wanted.

    18.4 gives that row no class at all, so it never reaches `classify_error`:
    the client answers it as the no-op it is, and the caller carries on.
    """
    adapter = build(
        lambda request: httpx.Response(
            400,
            json={
                "ok": False,
                "error_code": 400,
                "description": "Bad Request: message is not modified",
            },
        )
    )
    receipt = await adapter.edit(IDENTITY, "4242", "o mesmo texto")
    assert receipt.channel_message_id == "4242"


async def test_a_reaction_with_nothing_to_react_to_becomes_text() -> None:
    """The message was deleted between the ack and the reaction. Losing the
    acknowledgement to a 400 would be worse than saying it in words (18.4).
    """
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        calls.append(method)
        if method == "setMessageReaction":
            return httpx.Response(
                400,
                json={
                    "ok": False,
                    "error_code": 400,
                    "description": "Bad Request: message to react not found",
                },
            )
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 90, "date": 1}})

    adapter = build(handler)
    receipt = await adapter.send(
        IDENTITY, OutboundBlock(kind="reaction", emoji="👍", reply_to=("telegram", "4242"))
    )
    assert calls == ["setMessageReaction", "sendMessage"]
    assert receipt.channel_message_id == "90"


async def test_any_other_failure_to_react_is_not_swallowed() -> None:
    """Only the missing message degrades. A 401 is an account problem, and
    turning it into a text message would hide it behind a second failure.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401, json={"ok": False, "error_code": 401, "description": "Unauthorized"}
        )

    adapter = build(handler)
    with pytest.raises(TelegramApiError):
        await adapter.send(
            IDENTITY, OutboundBlock(kind="reaction", emoji="👍", reply_to=("telegram", "4242"))
        )
