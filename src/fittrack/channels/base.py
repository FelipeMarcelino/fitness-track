"""What the rest of the system is allowed to know about a messaging provider.

WhatsApp is the only channel today, and the point of this interface is that
nothing above it says so. The graph produces bubbles; the outbound queue orders
them; a Channel puts them on the wire. Telegram or a web client would be a new
implementation here and no change anywhere else.

Errors are the interesting part. Every provider fails in provider-specific
ways, but the §18.5 policy is written in terms of codes, so a channel's job is
to translate its failure into a code and a status and let the policy decide.
"""

from __future__ import annotations

from typing import Any, Protocol


class SendError(Exception):
    """A send that did not happen, described in terms the policy understands.

    `code` is the provider's error code when there is one. `status` is the HTTP
    status when there is one. A timeout has neither, and that absence is itself
    the classification: we do not know whether the message went out.
    """

    def __init__(
        self,
        message: str,
        code: str | None = None,
        status: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


class Channel(Protocol):
    """Sends one bubble and returns the provider's id for it."""

    async def send(self, bsuid: str, kind: str, payload: dict[str, Any]) -> str: ...
