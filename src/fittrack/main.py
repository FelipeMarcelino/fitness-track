"""FastAPI ingress.

Sprint 01 exposes /health and the WhatsApp webhook. The graph arrives in
feat/echo-graph; until then the buffer is where a delivery stops.
"""

from __future__ import annotations

import base64
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Literal

import redis.asyncio as aioredis
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import create_async_engine

from fittrack.channels.whatsapp.ingest import Ingest
from fittrack.channels.whatsapp.webhook import router as whatsapp_router
from fittrack.crypto.aesgcm import Encryptor, KeyRing
from fittrack.services.debounce import BurstBuffer
from fittrack.settings import Settings, get_settings

log = logging.getLogger(__name__)


def build_ingest(settings: Settings) -> tuple[Ingest, aioredis.Redis]:
    key = base64.b64decode(settings.fittrack_encryption_key.get_secret_value())
    encryptor = Encryptor(KeyRing({1: key}, current_version=1))
    engine = create_async_engine(settings.database_url.get_secret_value())
    # redis-py ships from_url untyped; the client itself is typed.
    redis: aioredis.Redis = aioredis.from_url(  # type: ignore[no-untyped-call]
        settings.redis_url.get_secret_value(),
        decode_responses=True,
    )
    buffer = BurstBuffer(redis, window_seconds=settings.debounce_window_s)
    return Ingest(engine, encryptor, buffer), redis


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Resolve configuration and build dependencies once, at startup.

    A bad credential crashes here, loudly, rather than on the first message --
    and without this the webhook would raise AttributeError on every delivery,
    which is how Meta decides to disable a webhook.
    """
    settings = get_settings()
    app.state.settings = settings
    app.state.ingest, redis = build_ingest(settings)
    try:
        yield
    finally:
        await redis.aclose()


app = FastAPI(title="FitTrack", docs_url=None, redoc_url=None, lifespan=lifespan)
app.include_router(whatsapp_router)


@app.get("/health")
async def health() -> dict[str, Literal["ok"]]:
    """Liveness. Deliberately does not touch Postgres or Redis: a health probe
    that fans out to dependencies turns one slow dependency into a restart
    loop. Dependency health belongs in /ready."""
    return {"status": "ok"}
