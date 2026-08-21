"""The Cloud API webhook (§18.1, §18.2).

Two hard requirements shape this module:

- Answer 200 in under 200 ms. Meta disables webhooks that are slow or that keep
  failing, and a disabled webhook is a dead bot.
- Verify the signature before anything else. The endpoint is public.

Everything past validation is handed to the queue. Nothing that can block --
database writes, media downloads, LLM calls -- happens on the response path.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import APIRouter, Header, HTTPException, Query, Request, Response, status

from fittrack.channels.whatsapp.payload import parse
from fittrack.channels.whatsapp.signature import verify_signature
from fittrack.settings import Settings, get_settings

log = logging.getLogger(__name__)
router = APIRouter(prefix="/webhook", tags=["whatsapp"])


@router.get("/whatsapp")
async def verify(
    mode: Annotated[str | None, Query(alias="hub.mode")] = None,
    token: Annotated[str | None, Query(alias="hub.verify_token")] = None,
    challenge: Annotated[str | None, Query(alias="hub.challenge")] = None,
) -> Response:
    """Meta's one-time subscription handshake: echo the challenge back."""
    settings = get_settings()
    expected = settings.waba_verify_token.get_secret_value()

    if mode != "subscribe" or token != expected:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "verification failed")

    return Response(content=challenge or "", media_type="text/plain")


@router.post("/whatsapp", status_code=status.HTTP_200_OK)
async def receive(
    request: Request,
    x_hub_signature_256: Annotated[str | None, Header()] = None,
) -> dict[str, str]:
    """Accepts a delivery, enqueues it, and returns.

    Returns 200 for anything that is correctly signed, including payloads we do
    not understand. Meta retries whatever is not acknowledged, so answering
    non-200 to a malformed delivery buys a retry storm and no information.
    """
    body = await request.body()
    settings = get_settings()

    if not verify_signature(
        body, x_hub_signature_256, settings.waba_app_secret.get_secret_value().encode()
    ):
        # No detail in the message: a precise error tells a prober what to fix.
        log.warning("rejected webhook delivery with an invalid signature")
        raise HTTPException(status.HTTP_403_FORBIDDEN, "invalid signature")

    envelope = await _json(request)
    messages, statuses = parse(envelope)

    ingest = request.app.state.ingest
    for message in messages:
        await ingest.accept_message(message, envelope)
    for update in statuses:
        await ingest.accept_status(update)

    return {"status": "ok"}


async def _json(request: Request) -> dict[str, Any]:
    try:
        payload = await request.json()
    except ValueError:
        log.warning("webhook delivery carried a body that is not JSON")
        return {}
    return payload if isinstance(payload, dict) else {}


def app_secret(settings: Settings) -> bytes:
    return settings.waba_app_secret.get_secret_value().encode()
