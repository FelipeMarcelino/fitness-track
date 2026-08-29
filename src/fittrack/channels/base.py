"""The contract every channel implements, and the types that cross the boundary.

Spec 18.1 defines two pieces and this module holds both. The `Channel` protocol
is what a channel must know how to do; `ChannelCaps` is what a channel *can* do,
so that the one agent allowed to care — `voice_agent` — chooses a format from a
descriptor instead of from a channel name (AD-39).

Everything else here is a value that travels between the protocol and the
domain. They are frozen, and their collections are immutable, because they are
handed across a queue, a buffer and a graph: a list that one stage appends to is
a bug that surfaces two stages later.

Two rules are enforced in code rather than trusted:

- **`reply_to` carries its channel.** A tenant may have Telegram and WhatsApp
  linked to the same history (spec 1.3), and the two message id spaces say
  nothing about each other. `ensure_addressable` rejects the mismatch before an
  adapter opens a connection.
- **`external_id` never reprs.** It is opaque, encrypted at rest, and the single
  most sensitive identifier in the system (spec 20.6, invariant 10). A repr is
  one exception away from a log line. The inbound payload and the message text
  are held to the same rule, because `channel.payload` and `user.text` are on
  the same redaction list (spec 20.2) and the raw update carries the
  `external_id` inside it.
  The same rule runs outbound: an `OutboundBlock` keeps its text and its
  buttons out of a repr, and a `TemplateRef` its parameters.
- **The inbound payload is a snapshot.** `raw` is deep-copied on the way in. The
  `Mapping` annotation is a promise to the reader and nothing to the runtime,
  and an adapter ordinarily keeps the dict it parsed: without the copy, an edit
  in that tree rewrites the audit record that `raw_message` is supposed to hold
  (invariant 6).
"""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, ClassVar, Literal, Protocol, runtime_checkable

# One definition of the channel names, and it is the one the settings validate
# and the `channel_kind` enum in the database mirrors (spec 5.2). A second
# Literal here would be a second thing to keep in step.
from fittrack.settings import ChannelKind

__all__ = [
    "Channel",
    "ChannelAuthenticationError",
    "ChannelCaps",
    "ChannelError",
    "ChannelIdentity",
    "ChannelMismatchError",
    "ClassifiedError",
    "ErrorClass",
    "InboundMessage",
    "OutboundBlock",
    "SendReceipt",
    "TemplateRef",
    "ensure_addressable",
]


class ChannelError(Exception):
    """Anything the channel layer refuses to do."""


class ChannelAuthenticationError(ChannelError):
    """An inbound request did not prove it came from the channel.

    Raised by `verify` before any parsing. The ingress answers 403 on this type
    alone, which is how it stays free of concrete adapter imports (spec 18.2).
    """


class ChannelMismatchError(ChannelError):
    """A block was addressed with another channel's identifiers."""


class ErrorClass(StrEnum):
    """What a send failure means, and therefore whether it may be repeated.

    The taxonomy is spec 18.4. `classify_error` translates each API's own
    vocabulary into these six values, so the outbound service decides on retry
    policy without knowing a single channel error code.
    """

    RETRY_BACKOFF = "retry_backoff"  # transient: repeat on the ladder
    RETRY_AFTER = "retry_after"  # the channel said when: obey it literally
    DEFER_WINDOW = "defer_window"  # outside the window: defer or templatise
    UNDELIVERABLE = "undeliverable"  # recipient is gone: suspend proactives
    ACCOUNT = "account"  # account problem: operational alert
    BUG = "bug"  # invalid payload: ours, log and alert


@dataclass(frozen=True, slots=True)
class ClassifiedError:
    """A classified send failure, with the number the channel supplied.

    Spec 18.1 types `classify_error` as returning the class alone. It returns
    this instead, because `RETRY_AFTER` is worthless without the seconds that
    came with it: the outbound service has to write `next_retry_at` and must not
    reach into a channel's exception to find the value (sprint S02-T06). The
    class is the same enum; this only keeps its evidence attached.
    """

    error_class: ErrorClass
    retry_after: int | None = None
    code: str | None = None

    def __post_init__(self) -> None:
        # Both directions. The class *is* the number: an adapter that saw a 429
        # without one has RETRY_BACKOFF and its ladder, and letting the verdict
        # through without the seconds moves the discovery to the moment the
        # outbound service writes `next_retry_at` (spec 18.4).
        if (self.retry_after is not None) != (self.error_class is ErrorClass.RETRY_AFTER):
            raise ValueError(
                f"retry_after is set if and only if the class is {ErrorClass.RETRY_AFTER}; "
                f"got {self.error_class} with retry_after={self.retry_after}"
            )
        # `next_retry_at = now + retry_after` is what the outbound service
        # computes from this. A negative number is a time already past, and the
        # worker would repeat the request the channel had just rate-limited,
        # immediately and every time — a sign slip becomes a retry loop.
        if self.retry_after is not None and self.retry_after < 0:
            raise ValueError(
                f"retry_after is a wait, not a deadline in the past: {self.retry_after}"
            )


@dataclass(frozen=True, slots=True)
class ChannelCaps:
    """What a channel can do (spec 18.1).

    Read in exactly two places — `voice_agent` and the output adapter — and the
    architecture test enforces that. `max_bubbles` is the odd one out: it is a
    product ceiling (spec 13.6), not an API limit, and it lives here so nobody
    "corrects" it thinking the platform imposed it.
    """

    reactions: bool
    reaction_set: Literal["arbitrary", "restricted"] | None
    buttons: bool
    max_buttons: int
    text_limit: int  # characters per message
    caption_limit: int  # characters in a media caption
    typing_indicator: bool
    edit_message: bool
    delete_message: bool
    proactive: Literal["free", "windowed"]
    window_hours: int | None  # None when proactive == "free"
    media_upload: Literal["inline", "two_step"]
    markup: Literal["telegram_html", "whatsapp_basic"]
    max_bubbles: int

    def __post_init__(self) -> None:
        # The two coherences the spec states in a comment. A descriptor that
        # claims a 24h window on a free channel would send the proactive coach
        # down the wrong branch, silently.
        if (self.window_hours is None) != (self.proactive == "free"):
            raise ValueError("window_hours is set if and only if proactive == 'windowed'")
        # Also both directions: `reactions=True` with no set tells the ack
        # formatter it may react and nothing about what with, so it picks an
        # emoji the channel rejects — Telegram takes a fixed list, WhatsApp
        # takes any (spec 18.1).
        if self.reactions != (self.reaction_set is not None):
            raise ValueError(
                "reaction_set is set if and only if reactions is true; "
                f"got reactions={self.reactions} with reaction_set={self.reaction_set!r}"
            )


@dataclass(frozen=True, slots=True)
class ChannelIdentity:
    """Where a message goes: an account on a channel, and the tenant behind it.

    `external_id` is the plaintext the adapter addresses (a Telegram `chat.id`,
    a WhatsApp BSUID). It is decrypted for the duration of a send and never
    printed — hence `repr=False`, which is the difference between a leak and a
    non-event the day someone logs an exception with this in the frame.
    """

    identity_id: int
    tenant_id: int
    channel: ChannelKind
    external_id: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class InboundMessage:
    """One message as the domain sees it, whichever channel produced it."""

    channel: ChannelKind
    external_id: str = field(repr=False)
    channel_message_id: str
    kind: Literal["text", "voice", "button_reply", "image", "document", "other"]
    # What the user wrote is `user.text` on the redaction list (spec 20.2). It
    # belongs in Langfuse, where it is put deliberately — never in a repr, which
    # is how it would arrive somewhere nobody chose.
    text: str | None = field(repr=False)
    media_ref: str | None  # file_id | media_id
    button_payload: str | None
    sent_at: datetime
    # The original payload, on its way to `raw_message` (invariant 6). A
    # `Mapping` rather than the spec's `dict`, and deep-copied below: the
    # annotation says "do not mutate" and the copy makes it so. It never reprs
    # either — it is `channel.payload`, and it contains the `external_id`.
    raw: Mapping[str, Any] = field(repr=False)

    def __post_init__(self) -> None:
        # A snapshot, taken once, at the only boundary that sees the update.
        # Left as a plain dict on purpose: this value is serialised to JSON on
        # its way to `raw_message`, and a mapping proxy is neither JSON- nor
        # pickle-serialisable, which would trade a mutation nobody makes for a
        # failure in a worker.
        object.__setattr__(self, "raw", deepcopy(dict(self.raw)))


@dataclass(frozen=True, slots=True)
class TemplateRef:
    """A pre-approved message and its parameters (spec 14.5).

    Windowed channels only. Telegram never produces one, which is exactly why
    the reference lives in the shared block rather than in a WhatsApp module.
    """

    name: str
    language: str  # e.g. "pt_BR"
    # Which template is operational; what goes in it is the user's name and
    # their numbers (spec 14.5), so the parameters follow the same rule as the
    # rest of the content on this boundary.
    parameters: tuple[str, ...] = field(default=(), repr=False)


@dataclass(frozen=True, slots=True)
class OutboundBlock:
    """One bubble the user will see, before any channel has formatted it.

    `buttons` is a tuple rather than the spec's list for the same reason as
    `InboundMessage.raw`: the block is frozen and shared.
    """

    kind: Literal["text", "reaction", "buttons", "media", "template"]
    # The outbound half of the repr rule. What the coach says is `llm.response`
    # on the redaction list (spec 20.2), and the clarification options carry
    # private exercise names — sanitising the inbound repr and leaving this one
    # open moves the leak downstream rather than closing it. The emoji and the
    # kind stay: they say what happened without saying what was said.
    text: str | None = field(default=None, repr=False)
    emoji: str | None = None
    buttons: tuple[str, ...] | None = field(default=None, repr=False)
    media_path: Path | None = None
    reply_to: tuple[ChannelKind, str] | None = None  # (channel, channel_message_id)
    template: TemplateRef | None = None  # windowed channels only


@dataclass(frozen=True, slots=True)
class SendReceipt:
    """Proof a bubble left, and what the next one needs to know.

    `media_ref` is the identifier the channel gives back for an uploaded file
    (a Telegram `file_id`, a Meta `media_id`). Keeping it makes a later retry
    cheap: the bytes do not go up twice (spec 18.2).
    """

    channel: ChannelKind
    channel_message_id: str
    sent_at: datetime
    media_ref: str | None = None


def ensure_addressable(kind: ChannelKind, identity: ChannelIdentity, block: OutboundBlock) -> None:
    """Refuse to send a block that names another channel.

    This is not type pedantry. A tenant with both channels linked has two
    message id spaces, and without the channel beside the id nothing stops the
    system from reacting to a Telegram message using a WhatsApp `message_id` —
    a request that either fails obscurely or, worse, succeeds against whatever
    message happens to hold that id (spec 18.1).

    Called first thing in `send`, so the rejection happens before any HTTP.
    """
    if identity.channel != kind:
        raise ChannelMismatchError(
            f"{kind} adapter cannot send to a {identity.channel} identity "
            f"(identity_id={identity.identity_id})"
        )
    if block.reply_to is not None and block.reply_to[0] != kind:
        raise ChannelMismatchError(
            f"{kind} adapter cannot reply to a {block.reply_to[0]} message; "
            "a message id means nothing outside the channel that issued it"
        )


@runtime_checkable
class Channel(Protocol):
    """What every channel knows how to do (spec 18.1).

    `runtime_checkable` gives the registry and the tests a cheap presence check;
    the shapes are mypy's job, and strict mode is what actually holds an adapter
    to this.
    """

    kind: ClassVar[ChannelKind]
    caps: ClassVar[ChannelCaps]

    # --- inbound ---------------------------------------------------------- #

    def verify(self, headers: Mapping[str, str], raw_body: bytes) -> None:
        """Prove the request came from the channel, in constant time.

        Raises `ChannelAuthenticationError` before anything is parsed.
        """
        ...

    def parse(self, payload: Mapping[str, Any]) -> list[InboundMessage]:
        """Turn one update into zero or more messages the domain understands."""
        ...

    async def download_media(self, media_ref: str) -> Path:
        """Fetch referenced media to tmpfs, returning the local path."""
        ...

    # --- outbound --------------------------------------------------------- #

    async def send(self, identity: ChannelIdentity, block: OutboundBlock) -> SendReceipt:
        """Deliver one block, after `ensure_addressable`."""
        ...

    def classify_error(self, exc: Exception) -> ClassifiedError:
        """Translate a channel failure into the shared taxonomy (spec 18.4)."""
        ...
