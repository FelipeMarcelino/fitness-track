"""Turning the Cloud API's answers into something §18.5 can act on."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from fittrack.channels.base import SendError
from fittrack.channels.whatsapp.client import WhatsAppChannel


def _channel(handler: Any) -> WhatsAppChannel:
    transport = httpx.MockTransport(handler)
    return WhatsAppChannel(
        "PHONE-ID", "token", client=httpx.AsyncClient(transport=transport, timeout=1.0)
    )


async def test_a_send_returns_the_provider_message_id() -> None:
    """Later delivery-status webhooks refer back to this id."""
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        seen["auth"] = request.headers.get("Authorization")
        return httpx.Response(200, json={"messages": [{"id": "wamid.XYZ"}]})

    assert await _channel(handler).send("BSUID-1", "text", {"body": "oi"}) == "wamid.XYZ"
    assert seen["url"].endswith("/PHONE-ID/messages")
    assert seen["auth"] == "Bearer token"
    assert seen["body"]["messaging_product"] == "whatsapp"
    # The BSUID goes out exactly as Meta delivered it (§18.4): it is not a
    # phone number and must not be reformatted.
    assert seen["body"]["to"] == "BSUID-1"
    assert seen["body"]["text"] == {"body": "oi"}


async def test_a_meta_error_keeps_its_code() -> None:
    """The code is the whole input to the §18.5 policy. Losing it turns every
    failure into the same failure."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={"error": {"code": 131047, "message": "Message failed to send"}},
        )

    with pytest.raises(SendError) as caught:
        await _channel(handler).send("BSUID-1", "text", {"body": "oi"})

    assert caught.value.code == "131047"
    assert caught.value.status == 400


async def test_an_unparseable_error_body_carries_no_code() -> None:
    """And that absence is the classification: an unknown code is not retried,
    which is the right answer for a response we do not understand."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, content=b"<html>gateway</html>")

    with pytest.raises(SendError) as caught:
        await _channel(handler).send("BSUID-1", "text", {"body": "oi"})

    assert caught.value.code is None
    assert caught.value.status == 400


async def test_a_timeout_has_neither_code_nor_status() -> None:
    """We do not know whether the message went out, and §18.5 leans on exactly
    that absence to decide it is retryable."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("too slow", request=request)

    with pytest.raises(SendError) as caught:
        await _channel(handler).send("BSUID-1", "text", {"body": "oi"})

    assert caught.value.code is None
    assert caught.value.status is None


async def test_a_5xx_keeps_its_status_so_it_can_be_retried() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={})

    with pytest.raises(SendError) as caught:
        await _channel(handler).send("BSUID-1", "text", {"body": "oi"})

    assert caught.value.status == 503
    assert caught.value.code is None


async def test_a_send_accepted_without_an_id_is_still_a_send() -> None:
    """The message did go out. Failing here would make the dispatcher retry a
    message the user has already received."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"messages": []})

    assert await _channel(handler).send("BSUID-1", "text", {"body": "oi"}) == ""
