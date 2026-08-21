"""WhatsApp Cloud API client (§18).

Only the send half lives here; receiving is the webhook's job. Everything this
module knows how to do is turn a bubble into one POST and turn the answer --
success or failure -- into something §18.5 can act on.

The `to` field carries the BSUID exactly as Meta delivered it in the webhook
(§18.4). It is not a phone number and must not be parsed, formatted, or shown
to the user.
"""

from __future__ import annotations

import logging
from typing import Any, Final

import httpx

from fittrack.channels.base import SendError

log = logging.getLogger(__name__)

GRAPH_VERSION: Final = "v21.0"

# Long enough for a normal send, short enough that a stuck call does not hold
# the bubble's claim lease open until it expires.
DEFAULT_TIMEOUT: Final = 15.0


class WhatsAppChannel:
    """Posts bubbles to the Cloud API."""

    def __init__(
        self,
        phone_number_id: str,
        access_token: str,
        client: httpx.AsyncClient | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self._phone_number_id = phone_number_id
        self._token = access_token
        self._client = client or httpx.AsyncClient(timeout=timeout)

    @property
    def url(self) -> str:
        return f"https://graph.facebook.com/{GRAPH_VERSION}/{self._phone_number_id}/messages"

    async def send(self, bsuid: str, kind: str, payload: dict[str, Any]) -> str:
        body = {"messaging_product": "whatsapp", "to": bsuid, "type": kind, kind: payload}
        try:
            response = await self._client.post(
                self.url,
                json=body,
                headers={"Authorization": f"Bearer {self._token}"},
            )
        except httpx.TimeoutException as exc:
            # Neither code nor status: we genuinely do not know whether the
            # message went out, and §18.5 treats that as retryable.
            raise SendError(f"timed out sending to the Cloud API: {exc}") from exc
        except httpx.HTTPError as exc:
            raise SendError(f"transport failure sending to the Cloud API: {exc}") from exc

        if response.status_code >= 400:
            raise _failure(response)

        return _message_id(response)


def _failure(response: httpx.Response) -> SendError:
    """Pulls Meta's error code out of the body.

    A 4xx whose body we cannot parse is deliberately left without a code: the
    policy's default for an unknown code is not to retry, which is the right
    answer for a response we do not understand.
    """
    code: str | None = None
    message = f"HTTP {response.status_code}"
    try:
        error = response.json().get("error", {})
    except ValueError:
        error = {}
    if isinstance(error, dict):
        raw = error.get("code")
        if raw is not None:
            code = str(raw)
        message = str(error.get("message", message))
    return SendError(message, code=code, status=response.status_code)


def _message_id(response: httpx.Response) -> str:
    """The id Meta assigns, which later status webhooks refer back to.

    Missing it is not fatal for the send -- the message did go out -- but it
    breaks the delivery-status correlation, so it is worth a log line.
    """
    try:
        messages = response.json().get("messages") or []
    except ValueError:
        messages = []
    if messages and isinstance(messages[0], dict):
        found = messages[0].get("id")
        if found:
            return str(found)
    log.warning("the Cloud API accepted a send without returning a message id")
    return ""
