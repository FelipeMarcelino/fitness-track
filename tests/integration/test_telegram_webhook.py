"""Telegram ingress contract (Sprint 02, task S02-T03)."""

from __future__ import annotations

from collections.abc import Mapping

import httpx
import pytest

from fittrack.channels.base import ChannelAuthenticationError
from fittrack.main import create_app


class RecordingIngress:
    """Ingress double: transport tests must not need a Telegram client."""

    def __init__(self, *, reject: bool = False) -> None:
        self.reject = reject
        self.calls: list[tuple[dict[str, str], bytes]] = []

    def verify(self, headers: Mapping[str, str]) -> None:
        if self.reject:
            raise ChannelAuthenticationError

    async def receive(self, headers: Mapping[str, str], body: bytes) -> None:
        self.calls.append((dict(headers), body))


@pytest.mark.asyncio
async def test_telegram_webhook_endpoint_exists() -> None:
    """The public ingress starts with Telegram's sole POST route (spec 18.2)."""
    ingress = RecordingIngress()
    transport = httpx.ASGITransport(app=create_app(telegram_ingress=ingress))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/webhook/telegram", json={"update_id": 1})

    assert response.status_code == 200
    assert ingress.calls


async def test_telegram_webhook_rejects_an_invalid_secret_before_processing() -> None:
    """A forged request is 403 and must not reach parsing or storage (spec 18.2)."""
    ingress = RecordingIngress(reject=True)
    transport = httpx.ASGITransport(app=create_app(telegram_ingress=ingress))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/webhook/telegram", content=b'{"update_id": 1}')

    assert response.status_code == 403
    assert ingress.calls == []
