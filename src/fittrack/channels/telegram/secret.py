"""The webhook's shared secret (spec 18.2).

`setWebhook` registers a `secret_token`, and Telegram returns it in a header on
every update. Comparing it is the whole of the webhook's authentication: there
is no challenge route and no body signature, because Telegram signs nothing.

It proves origin, not integrity — but the transport is TLS and the origin is
verified, so the residual surface is the same as the WhatsApp HMAC's.
"""

from __future__ import annotations

import hmac
from typing import TYPE_CHECKING

from fittrack.channels.base import ChannelAuthenticationError

if TYPE_CHECKING:
    from collections.abc import Mapping

__all__ = ["SECRET_HEADER", "verify_secret_header"]

SECRET_HEADER = "X-Telegram-Bot-Api-Secret-Token"


def verify_secret_header(headers: Mapping[str, str], expected: str | None) -> None:
    """Raise unless the request carries the secret we registered.

    `hmac.compare_digest` rather than `==`, because `==` returns as soon as two
    bytes differ: the time it takes says how long the matching prefix was, and a
    few thousand requests turn that into the secret. The comparison runs even
    when the header is absent, so the absent and the wrong case cost the same.

    `expected` is None when this process polls instead of listening. There is no
    webhook then, so nothing arriving at one can be authentic.
    """
    if expected is None:
        raise ChannelAuthenticationError(
            f"{SECRET_HEADER} cannot be verified: this process has no webhook secret"
        )
    if not hmac.compare_digest(_header(headers, SECRET_HEADER), expected):
        # The message names the header and nothing else. Quoting either side of
        # a failed comparison publishes the secret to whoever reads the log.
        raise ChannelAuthenticationError(f"{SECRET_HEADER} does not match")


def _header(headers: Mapping[str, str], name: str) -> str:
    """One header, looked up without caring about case.

    ASGI hands the ingress a case-insensitive mapping, but a plain dict is not
    one, and this function is also called from tests and from `bootstrap`.
    """
    lowered = name.lower()
    for key, value in headers.items():
        if key.lower() == lowered:
            return value
    return ""
