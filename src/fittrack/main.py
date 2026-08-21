"""FastAPI ingress.

Sprint 01 ships only /health: the compose healthcheck depends on it, and the
webhook (§18) lands in feat/whatsapp-webhook.
"""

from __future__ import annotations

from typing import Literal

from fastapi import FastAPI

from fittrack.settings import get_settings

app = FastAPI(title="FitTrack", docs_url=None, redoc_url=None)


@app.get("/health")
async def health() -> dict[str, Literal["ok"]]:
    """Liveness. Deliberately does not touch Postgres or Redis: a health probe
    that fans out to dependencies turns one slow dependency into a restart loop.
    Dependency health belongs in /ready, which arrives with the webhook."""
    return {"status": "ok"}


@app.on_event("startup")
async def _validate_configuration() -> None:
    """Resolve settings once at startup so a bad credential crashes here, loudly,
    instead of on the first message."""
    get_settings()
