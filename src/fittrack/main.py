"""Ingress (spec 3.1): FastAPI in front of the channel webhooks.

Only the health endpoint exists so far. The Telegram webhook lands with the
`TelegramAdapter` in a later sprint, and nothing else may be published here:
no endpoint may reveal whether a tenant exists (spec 22).
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from typing import Literal, Protocol

from fastapi import FastAPI, HTTPException, Request, Response

from fittrack.channels.base import ChannelAuthenticationError
from fittrack.startup import startup


class TelegramIngress(Protocol):
    """The Telegram-specific work that happens behind the HTTP boundary.

    The route does not know an adapter, Redis or SQLAlchemy. That is what lets
    S02-T03 implement the ingress in parallel with the Telegram adapter in
    S02-T02 while keeping failed authentication outside every later step.
    """

    async def receive(self, headers: Mapping[str, str], body: bytes) -> None:
        """Verify and accept one Telegram update, or reject its secret."""


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    """Validate before serving. An invalid deployment must not report healthy."""
    startup("ingress")
    yield


def create_app(*, telegram_ingress: TelegramIngress | None = None) -> FastAPI:
    """Build the ingress app with a replaceable Telegram processing port."""
    app = FastAPI(
        title="FitTrack ingress",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )

    @app.get("/health")
    async def health() -> dict[str, Literal["ok"]]:
        """Liveness only. It must not touch Postgres, Redis or Qdrant."""
        return {"status": "ok"}

    @app.post("/webhook/telegram", status_code=200)
    async def telegram_webhook(request: Request) -> Response:
        """Accept a verified update without exposing any tenant information."""
        if telegram_ingress is None:
            raise HTTPException(status_code=503, detail="telegram ingress is unavailable")
        try:
            await telegram_ingress.receive(dict(request.headers), await request.body())
        except ChannelAuthenticationError:
            raise HTTPException(status_code=403, detail="forbidden") from None
        return Response(status_code=200)

    return app


app = create_app()
