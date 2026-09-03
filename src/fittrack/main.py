"""Ingress (spec 3.1): FastAPI in front of the channel webhooks.

No endpoint may reveal whether a tenant exists (spec 22).

The Telegram wiring lives in `fittrack.ingress_wiring`, not here, for the
reason `TelegramIngress` below already existed for: this module must not name
a concrete Telegram type (`tests/unit/test_channel_contract.py`), and it must
not read the environment at import time
(`tests/unit/test_entrypoints.py::test_the_entrypoints_import_without_an_environment`).
`lifespan` is where both constraints stop applying — it only runs once the
process is actually serving.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from typing import Literal, Protocol

from fastapi import FastAPI, HTTPException, Request, Response

from fittrack.channels.base import ChannelAuthenticationError
from fittrack.services.webhook import UpdateInFlightError
from fittrack.startup import startup


class TelegramIngress(Protocol):
    """The Telegram-specific work that happens behind the HTTP boundary.

    The route does not know an adapter, Redis or SQLAlchemy. That is what lets
    S02-T03 implement the ingress in parallel with the Telegram adapter in
    S02-T02 while keeping failed authentication outside every later step.
    """

    def verify(self, headers: Mapping[str, str]) -> None:
        """Reject a forged request before FastAPI buffers its body."""

    async def receive(self, headers: Mapping[str, str], body: bytes) -> None:
        """Verify and accept one Telegram update, or reject its secret."""


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Validate before serving, then wire Telegram if nothing already did.

    An invalid deployment must not report healthy — `startup` runs first and
    unconditionally. `app.state.telegram_ingress` is only built here when it is
    still `None`: a caller that passed one to `create_app` (every test in
    `tests/integration/test_telegram_webhook.py`) gets to keep it, since this
    context manager never runs for a transport that skips the ASGI lifespan
    protocol, and would otherwise silently overwrite it for one that doesn't.
    """
    settings, _config = startup("ingress")
    runtime = None
    if app.state.telegram_ingress is None:
        from fittrack.ingress_wiring import open_telegram_runtime

        runtime = await open_telegram_runtime(settings)
        if runtime is not None:
            app.state.telegram_ingress = runtime.ingress
    app.state.telegram_runtime = runtime
    try:
        yield
    finally:
        if runtime is not None:
            await runtime.aclose()


def create_app(*, telegram_ingress: TelegramIngress | None = None) -> FastAPI:
    """Build the ingress app with a replaceable Telegram processing port.

    `telegram_ingress` seeds `app.state`, which is what the route and
    `lifespan` both read — a test that injects a fake here is respected
    whether or not it ever triggers the ASGI lifespan protocol.
    """
    app = FastAPI(
        title="FitTrack ingress",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.telegram_ingress = telegram_ingress
    app.state.telegram_runtime = None

    @app.get("/health")
    async def health() -> dict[str, Literal["ok"]]:
        """Liveness only. It must not touch Postgres, Redis or Qdrant."""
        return {"status": "ok"}

    @app.post("/webhook/telegram", status_code=200)
    async def telegram_webhook(request: Request) -> Response:
        """Accept a verified update without exposing any tenant information."""
        ingress: TelegramIngress | None = request.app.state.telegram_ingress
        if ingress is None:
            raise HTTPException(status_code=503, detail="telegram ingress is unavailable")
        try:
            ingress.verify(request.headers)
            await ingress.receive(request.headers, await request.body())
        except ChannelAuthenticationError:
            raise HTTPException(status_code=403, detail="forbidden") from None
        except UpdateInFlightError:
            raise HTTPException(
                status_code=503, detail="telegram update is still processing"
            ) from None
        return Response(status_code=200)

    return app


app = create_app()
