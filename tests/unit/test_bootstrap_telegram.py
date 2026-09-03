"""The Telegram half of `scripts/bootstrap.py` (S02-T08, spec 18.2).

`setWebhook` and `deleteWebhook` are mutually exclusive with each other and
with a second poller (spec 18.2's 409 trap), so bootstrap is the one place
that reconciles `TELEGRAM_MODE` against what Telegram actually has registered
— every real deployment goes through it, and `--check` never does (module
docstring of `scripts/bootstrap.py`: a dry run changes nothing).

No test opens a socket: `TelegramClient` takes an `httpx.AsyncClient`, and
these hand it a `MockTransport`, the same pattern
`tests/unit/test_telegram_polling.py` uses.
"""

from __future__ import annotations

import json
import os
from typing import Any

import httpx
import pytest
from scripts.bootstrap import BootstrapError, reconcile_telegram

from fittrack.channels.telegram.polling import ALLOWED_UPDATES
from fittrack.settings import Settings
from tests.unit.test_startup import environment

MAX_CONNECTIONS = 40


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    """Same rule as `tests/unit/test_startup.py`'s: a fresh channel environment
    per test, not one file's leftovers deciding another's default.
    """
    for name in list(os.environ):
        if name.startswith(("FITTRACK_", "DATABASE_", "REDIS_", "QDRANT_", "TELEGRAM_", "WABA_")):
            monkeypatch.delenv(name, raising=False)
    return monkeypatch


class Scripted:
    """Records every Bot API call this test hands it, and answers `ok`."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        body: dict[str, Any] = json.loads(request.content) if request.content else {}
        self.calls.append((method, body))
        return httpx.Response(200, json={"ok": True, "result": True})


def settings_with(monkeypatch: pytest.MonkeyPatch, **overrides: str) -> Settings:
    for name, value in environment(**overrides).items():
        monkeypatch.setenv(name, value)
    return Settings(_env_file=None)


async def test_telegram_disabled_reconciles_nothing(clean_env: pytest.MonkeyPatch) -> None:
    settings = settings_with(clean_env, FITTRACK_CHANNELS="")

    result = await reconcile_telegram(settings)

    assert "not" in result and "enabled" in result


async def test_webhook_mode_registers_the_webhook(clean_env: pytest.MonkeyPatch) -> None:
    settings = settings_with(
        clean_env,
        FITTRACK_CHANNELS="telegram",
        TELEGRAM_BOT_TOKEN="123:abc",
        TELEGRAM_MODE="webhook",
        TELEGRAM_WEBHOOK_SECRET="s" * 43,
        TELEGRAM_WEBHOOK_URL="https://ingress.example.com/webhook/telegram",
    )
    script = Scripted()
    http = httpx.AsyncClient(transport=httpx.MockTransport(script.handler))

    result = await reconcile_telegram(settings, http=http)

    assert result == "webhook registered"
    assert script.calls[0][0] == "setWebhook"
    body = script.calls[0][1]
    assert body["url"] == "https://ingress.example.com/webhook/telegram"
    assert body["secret_token"] == "s" * 43
    assert body["allowed_updates"] == list(ALLOWED_UPDATES)
    assert body["max_connections"] == MAX_CONNECTIONS
    assert not http.is_closed  # injected: the caller owns it, not this function


async def test_polling_mode_deletes_the_webhook(clean_env: pytest.MonkeyPatch) -> None:
    settings = settings_with(
        clean_env,
        FITTRACK_CHANNELS="telegram",
        TELEGRAM_BOT_TOKEN="123:abc",
        TELEGRAM_MODE="polling",
    )
    script = Scripted()
    http = httpx.AsyncClient(transport=httpx.MockTransport(script.handler))

    result = await reconcile_telegram(settings, http=http)

    assert result == "webhook deleted; polling is ready"
    assert script.calls == [("deleteWebhook", {})]


async def test_a_refusal_from_telegram_is_reported_as_a_bootstrap_error(
    clean_env: pytest.MonkeyPatch,
) -> None:
    settings = settings_with(
        clean_env,
        FITTRACK_CHANNELS="telegram",
        TELEGRAM_BOT_TOKEN="123:abc",
        TELEGRAM_MODE="polling",
    )

    def refuse(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401, json={"ok": False, "error_code": 401, "description": "Unauthorized"}
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(refuse))

    with pytest.raises(BootstrapError, match="Unauthorized"):
        await reconcile_telegram(settings, http=http)


async def test_no_http_client_given_reconciles_telegram_disabled_without_opening_one(
    clean_env: pytest.MonkeyPatch,
) -> None:
    """The production path: `main()` passes nothing, so this owns the pool.

    Telegram disabled short-circuits before any client is built, which is the
    branch this proves without a live socket.
    """
    settings = settings_with(clean_env, FITTRACK_CHANNELS="")

    result = await reconcile_telegram(settings)

    assert "not" in result
