"""Ingress (spec 3.1): FastAPI in front of the channel webhooks.

Only the health endpoint exists so far. The Telegram webhook lands with the
`TelegramAdapter` in a later sprint, and nothing else may be published here:
no endpoint may reveal whether a tenant exists (spec 22).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import FastAPI

from fittrack.startup import startup


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    """Validate before serving. An invalid deployment must not report healthy."""
    startup("ingress")
    yield


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
