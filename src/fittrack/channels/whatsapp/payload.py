"""Parsing of the Cloud API webhook envelope (§18.3).

Meta nests messages three levels deep and mixes message deliveries with status
callbacks in the same shape. Everything here is defensive: the payload comes
from outside, and a KeyError becomes a 500, which Meta answers by disabling the
endpoint.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Types we act on. Anything else is acknowledged and ignored (§18.3).
ACTIONABLE = frozenset({"text", "audio", "interactive"})


@dataclass(frozen=True)
class InboundMessage:
    message_id: str
    bsuid: str
    msg_type: str
    timestamp: int
    text: str | None = None
    media_id: str | None = None
    button_id: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def is_actionable(self) -> bool:
        return self.msg_type in ACTIONABLE


@dataclass(frozen=True)
class StatusUpdate:
    message_id: str
    status: str
    recipient: str
    error_code: str | None = None


def parse(envelope: dict[str, Any]) -> tuple[list[InboundMessage], list[StatusUpdate]]:
    """Flattens the envelope into messages and status updates.

    Never raises on shape: an unexpected payload yields empty lists, which the
    caller acknowledges with 200. Meta retries anything it does not get a 200
    for, so raising would turn one malformed delivery into a retry storm.
    """
    messages: list[InboundMessage] = []
    statuses: list[StatusUpdate] = []

    for entry in _list(envelope.get("entry")):
        for change in _list(entry.get("changes")):
            value = change.get("value")
            if not isinstance(value, dict):
                continue
            messages.extend(_messages(value))
            statuses.extend(_statuses(value))

    return messages, statuses


def _messages(value: dict[str, Any]) -> list[InboundMessage]:
    out: list[InboundMessage] = []
    for message in _list(value.get("messages")):
        message_id = message.get("id")
        sender = message.get("from")
        msg_type = message.get("type")
        if not (message_id and sender and msg_type):
            continue

        out.append(
            InboundMessage(
                message_id=str(message_id),
                bsuid=str(sender),
                msg_type=str(msg_type),
                timestamp=int(message.get("timestamp") or 0),
                text=_dig(message, "text", "body"),
                media_id=_dig(message, "audio", "id"),
                button_id=_dig(message, "interactive", "button_reply", "id"),
                raw=message,
            )
        )
    return out


def _statuses(value: dict[str, Any]) -> list[StatusUpdate]:
    out: list[StatusUpdate] = []
    for status in _list(value.get("statuses")):
        message_id = status.get("id")
        state = status.get("status")
        if not (message_id and state):
            continue
        errors = _list(status.get("errors"))
        out.append(
            StatusUpdate(
                message_id=str(message_id),
                status=str(state),
                recipient=str(status.get("recipient_id") or ""),
                error_code=str(errors[0].get("code")) if errors else None,
            )
        )
    return out


def _list(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _dig(source: dict[str, Any], *keys: str) -> str | None:
    current: Any = source
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return str(current) if isinstance(current, str) else None
