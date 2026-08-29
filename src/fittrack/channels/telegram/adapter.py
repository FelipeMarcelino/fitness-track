"""The `Channel` for Telegram (spec 18.2, 18.4, 13.2).

Two translations, in opposite directions, and one judgement.

Inbound, `parse` turns an update into zero or more `InboundMessage`. Zero is a
real answer: `allowed_updates` narrows what arrives (spec 18.2), and anything
that slips past it is ignored silently rather than raising in the ingress. What
we *do* recognise is always returned, even when nothing downstream will act on
it — a reaction update and a block both become an `other` message so the payload
reaches `raw_message` and the ingress decides (invariant 6, spec 18.2).

Outbound, `send` turns one `OutboundBlock` into one API call, after
`ensure_addressable` and before any HTTP.

The judgement is `classify_error`, which is where Telegram's vocabulary stops.
The outbound service reads a class and never a status code, which is what lets
one retry policy serve two channels that disagree about what failure means.

One piece of protocol knowledge lives here on purpose: Telegram takes one emoji
from a fixed list, and 13.2 says an emoji outside it degrades to text *in the
adapter*. The agent chooses to acknowledge; which emoji a channel accepts is not
its business.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Literal

from fittrack.channels.base import (
    ChannelCaps,
    ChannelError,
    ClassifiedError,
    ErrorClass,
    InboundMessage,
    SendReceipt,
    ensure_addressable,
    ensure_identity,
)
from fittrack.channels.telegram.client import (
    TelegramApiError,
    TelegramClient,
    TelegramTransportError,
)
from fittrack.channels.telegram.markup import clip_caption, inline_keyboard, to_telegram_html
from fittrack.channels.telegram.secret import verify_secret_header

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from pydantic import SecretStr

    from fittrack.channels.base import ChannelIdentity, OutboundBlock
    from fittrack.settings import ChannelKind

__all__ = ["MAX_AUDIO_SECONDS", "TELEGRAM_REACTIONS", "TelegramAdapter"]

# The kinds of 18.1, named once so the parser's return type is the same closed
# set the dataclass field is.
type InboundKind = Literal["text", "voice", "button_reply", "image", "document", "other"]

logger = logging.getLogger(__name__)

# Section 11.3: five minutes. Past it the pipeline asks the user to split rather
# than paying for a transcription that will be poor anyway.
MAX_AUDIO_SECONDS = 300

# Media lands in tmpfs and is deleted after transcription (spec 11.1). Never a
# persistent volume: the recording is the user's voice.
DEFAULT_DOWNLOAD_DIR = Path("/tmp")

# The emoji Telegram accepts on `setMessageReaction`. It is a fixed, published
# list, and `✅` is not in it — which is exactly why 13.2 keeps a map per channel
# instead of hoping.
_REACTION_EMOJI = (
    "👍 👎 ❤ 🔥 🥰 👏 😁 🤔 🤯 😱 🤬 😢 🎉 🤩 🤮 💩 🙏 👌 🕊 🤡 🥱 🥴 😍 "
    "🐳 🌚 🌭 💯 🤣 ⚡ 🍌 🏆 💔 🤨 😐 🍓 🍾 💋 🖕 😈 😴 😭 🤓 👻 👀 🎃 🙈 "
    "😇 😨 🤝 ✍ 🤗 🫡 🎅 🎄 ☃ 💅 🤪 🗿 🆒 💘 🙉 🦄 😘 💊 🙊 😎 👾 🤷 😡"
)
TELEGRAM_REACTIONS = frozenset(_REACTION_EMOJI.split())

TELEGRAM_CAPS = ChannelCaps(
    reactions=True,
    reaction_set="restricted",
    buttons=True,
    max_buttons=8,
    text_limit=4096,
    caption_limit=1024,
    typing_indicator=True,
    edit_message=True,
    delete_message=True,
    proactive="free",
    window_hours=None,
    media_upload="inline",
    markup="telegram_html",
    max_bubbles=3,
)

# Descriptions from 18.4 that mean the recipient is gone rather than that the
# request was wrong. Matched as substrings because Telegram prefixes them.
_UNDELIVERABLE = (
    "bot was blocked by the user",
    "user is deactivated",
    "chat not found",
    "bot can't initiate conversation",
)
_NOTHING_TO_REACT_TO = "message to react not found"

# Marks a `channel_message_id` that identifies an event rather than a message.
# Neither can be addressed, and both are numbers in Telegram's own vocabulary.
PRESS_PREFIX = "press:"
UPDATE_PREFIX = "update:"
# And a reaction, which is a mark on somebody else's message rather than a
# message of ours: there is nothing there for `editMessageText` to rewrite.
REACTION_PREFIX = "reaction:"


class TelegramAdapter:
    """Telegram, behind the `Channel` protocol."""

    kind: ClassVar[ChannelKind] = "telegram"
    caps: ClassVar[ChannelCaps] = TELEGRAM_CAPS

    def __init__(
        self,
        client: TelegramClient,
        *,
        webhook_secret: SecretStr | None = None,
        download_dir: Path | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._client = client
        self._webhook_secret = webhook_secret
        self._download_dir = download_dir or DEFAULT_DOWNLOAD_DIR
        self._now = now

    def __repr__(self) -> str:
        # Holds the token and the shared secret. Neither has any business in a
        # traceback frame (spec 20.6).
        return "TelegramAdapter(kind='telegram')"

    # --- inbound ----------------------------------------------------------- #

    def verify(self, headers: Mapping[str, str], raw_body: bytes) -> None:
        """Prove the update came from Telegram, before anything parses it."""
        secret = self._webhook_secret.get_secret_value() if self._webhook_secret else None
        verify_secret_header(headers, secret)

    def parse(self, payload: Mapping[str, Any]) -> list[InboundMessage]:
        """One update, as zero or one messages the domain understands."""
        if message := payload.get("message"):
            if not _is_private(message.get("chat")):
                return []
            return [self._from_message(payload, message)]
        if press := payload.get("callback_query"):
            if not _is_private((press.get("message") or {}).get("chat")):
                return []
            return [self._from_callback(payload, press)]
        # Recognised but not processed. Both still become a message so the
        # payload reaches `raw_message`: the reaction because 18.2 ignores it,
        # the membership change because revoking the identity is the ingress's
        # job and it needs the event to do it.
        for name in ("message_reaction", "my_chat_member"):
            if event := payload.get(name):
                # The same private-chat rule. `my_chat_member` is exactly what
                # arrives when the bot is added to a group, and the ingress
                # resolves identity before it filters these — so an unguarded
                # event would mint an identity for the group's shared `chat.id`.
                if not _is_private(event.get("chat")):
                    return []
                return [self._from_event(payload, event)]
        return []

    async def download_media(self, media_ref: str) -> Path:
        """`getFile`, then the bytes, into tmpfs (spec 11.1)."""
        described = await self._client.call("getFile", {"file_id": media_ref})
        file_path = str(described["file_path"])
        where = await self._client.download(file_path, into=self._download_dir)
        # The size is safe to log; the path is the access secret and is not.
        logger.debug(
            "telegram media downloaded",
            extra={"bytes": where.stat().st_size, "channel": "telegram"},
        )
        return where

    # --- outbound ---------------------------------------------------------- #

    async def send(self, identity: ChannelIdentity, block: OutboundBlock) -> SendReceipt:
        """Deliver one block. The guard runs before the first byte leaves."""
        ensure_addressable(self.kind, identity, block)
        match block.kind:
            case "text":
                return await self._send_text(identity, block)
            case "reaction":
                return await self._send_reaction(identity, block)
            case "buttons":
                return await self._send_buttons(identity, block)
            case "media":
                return await self._send_media(identity, block)
            case "template":
                raise ChannelError(
                    "telegram has no templates: they belong to the windowed channels (spec 14.5)"
                )
            case _:  # pragma: no cover - the Literal is closed
                raise ChannelError(f"telegram cannot send a {block.kind} block")

    async def answer_callback(self, callback_query_id: str) -> None:
        """Stop the button spinning, before anything is queued (spec 18.2).

        Telegram leaves the client's progress indicator turning until the
        callback is answered or it times out, whatever the pipeline does in the
        meantime. `parse` is synchronous by the protocol of 18.1 and cannot make
        the call, so the ingress makes it as soon as the update is verified.

        Takes the id as it was parsed: the `press:` prefix is ours, and Telegram
        wants the number it issued.
        """
        await self._client.call(
            "answerCallbackQuery",
            {"callback_query_id": callback_query_id.removeprefix(PRESS_PREFIX)},
        )

    async def send_typing(self, identity: ChannelIdentity) -> None:
        """ "typing…", which expires after about five seconds and is repeated."""
        ensure_identity(self.kind, identity)
        await self._client.call(
            "sendChatAction", {"chat_id": identity.external_id, "action": "typing"}
        )

    async def edit(self, identity: ChannelIdentity, message_id: str, text: str) -> SendReceipt:
        """Rewrite a message of ours — the one asymmetric capability (13.2).

        A correction just after an emoji ack edits the confirmation instead of
        adding a bubble. An edit that changes nothing answers `message is not
        modified`, which the client reports as the success it is.
        """
        ensure_identity(self.kind, identity)
        addressable = _addressable(message_id)
        await self._client.call(
            "editMessageText",
            {
                "chat_id": identity.external_id,
                "message_id": addressable,
                "text": to_telegram_html(text),
                "parse_mode": "HTML",
                "link_preview_options": {"is_disabled": True},
            },
        )
        return SendReceipt(channel=self.kind, channel_message_id=message_id, sent_at=self._now())

    def classify_error(self, exc: Exception) -> ClassifiedError:
        """Telegram's vocabulary, in the shared taxonomy (spec 18.4)."""
        if isinstance(exc, TelegramTransportError):
            return ClassifiedError(ErrorClass.RETRY_BACKOFF)
        if not isinstance(exc, TelegramApiError):
            # Not the channel's failure. Retrying our own TypeError would repeat
            # it on a schedule.
            return ClassifiedError(ErrorClass.BUG)

        code = str(exc.status_code)
        description = exc.description.lower()

        if exc.status_code == 429:
            seconds = _retry_after(exc.parameters)
            if seconds is None:
                # 18.4 waits exactly what the channel said; with no number there
                # is nothing to obey, and the ladder is the honest fallback.
                return ClassifiedError(ErrorClass.RETRY_BACKOFF, code=code)
            return ClassifiedError(ErrorClass.RETRY_AFTER, retry_after=seconds, code=code)
        if exc.status_code == 401:
            return ClassifiedError(ErrorClass.ACCOUNT, code=code)
        if exc.status_code >= 500:
            return ClassifiedError(ErrorClass.RETRY_BACKOFF, code=code)
        if any(reason in description for reason in _UNDELIVERABLE):
            return ClassifiedError(ErrorClass.UNDELIVERABLE, code=code)
        return ClassifiedError(ErrorClass.BUG, code=code)

    # --- inbound internals ------------------------------------------------- #

    def _from_message(
        self, payload: Mapping[str, Any], message: Mapping[str, Any]
    ) -> InboundMessage:
        kind, text, media_ref = _classify_message(message)
        return InboundMessage(
            channel=self.kind,
            external_id=str(message["chat"]["id"]),
            channel_message_id=str(message["message_id"]),
            kind=kind,
            text=text,
            media_ref=media_ref,
            button_payload=None,
            sent_at=datetime.fromtimestamp(int(message["date"]), tz=UTC),
            raw=payload,
        )

    def _from_callback(
        self, payload: Mapping[str, Any], press: Mapping[str, Any]
    ) -> InboundMessage:
        message = press.get("message") or {}
        chat = message.get("chat") or {}
        external_id = str(chat.get("id") or press["from"]["id"])
        return InboundMessage(
            channel=self.kind,
            external_id=external_id,
            # The press, not the message it sits under: two answers to one
            # clarification are two events, and `raw_message` is unique on
            # `(identity_id, channel_message_id)` (spec 17.4) — keying by the
            # message would swallow the second answer as a duplicate.
            #
            # Prefixed because the same field is what `reply_to` carries, and
            # Telegram numbers presses and messages alike: an unprefixed press
            # id would be sent as `reply_parameters.message_id` and land on
            # whatever message happens to hold that number. `send` refuses it.
            channel_message_id=f"{PRESS_PREFIX}{press['id']}",
            kind="button_reply",
            text=None,
            media_ref=None,
            button_payload=press.get("data"),
            # A callback query carries no date of its own; the webhook is
            # delivered immediately, so now is the closest true answer.
            sent_at=self._now(),
            raw=payload,
        )

    def _from_event(self, payload: Mapping[str, Any], event: Mapping[str, Any]) -> InboundMessage:
        chat = event.get("chat") or {}
        return InboundMessage(
            channel=self.kind,
            external_id=str(chat.get("id", "")),
            # The update, not the message it refers to: two reactions to one
            # message are two events, and `raw_message` is unique on
            # `(identity_id, channel_message_id)` (spec 17.4).
            channel_message_id=f"{UPDATE_PREFIX}{payload['update_id']}",
            kind="other",
            text=None,
            media_ref=None,
            button_payload=None,
            sent_at=datetime.fromtimestamp(int(event["date"]), tz=UTC)
            if "date" in event
            else self._now(),
            raw=payload,
        )

    # --- outbound internals ------------------------------------------------ #

    async def _send_text(self, identity: ChannelIdentity, block: OutboundBlock) -> SendReceipt:
        sent = await self._client.call(
            "sendMessage", self._text_payload(identity, block.text or "", block)
        )
        return self._receipt(sent)

    async def _send_buttons(self, identity: ChannelIdentity, block: OutboundBlock) -> SendReceipt:
        options = block.buttons or ()
        if len(options) > self.caps.max_buttons:
            # The voice agent degrades to a numbered list above the ceiling
            # (9.10), so arriving here with more is our bug — caught before the
            # request rather than as a 400 after it.
            raise ChannelError(
                f"{len(options)} buttons is over max_buttons={self.caps.max_buttons}; "
                "the clarification should have degraded to a numbered list (spec 9.10)"
            )
        payload = self._text_payload(identity, block.text or "", block)
        payload["reply_markup"] = inline_keyboard(options)
        return self._receipt(await self._client.call("sendMessage", payload))

    async def _send_reaction(self, identity: ChannelIdentity, block: OutboundBlock) -> SendReceipt:
        emoji = block.emoji or ""
        if block.reply_to is None:
            raise ChannelError("a reaction needs the message it reacts to (spec 18.2)")
        message_id = block.reply_to[1]
        addressable = _addressable(message_id)

        if emoji not in TELEGRAM_REACTIONS:
            # 13.2 puts this here: which emoji a channel takes is protocol, and
            # losing the acknowledgement to a 400 is worse than saying it in
            # text. No emoji is named in the log line — it is not sensitive, but
            # the count is what tells us the ack map has drifted.
            logger.info("telegram reaction degraded to text", extra={"channel": "telegram"})
            return await self._send_degraded(identity, emoji, block)

        try:
            await self._client.call(
                "setMessageReaction",
                {
                    "chat_id": identity.external_id,
                    "message_id": addressable,
                    "reaction": [{"type": "emoji", "emoji": emoji}],
                },
            )
        except TelegramApiError as error:
            if _NOTHING_TO_REACT_TO not in error.description.lower():
                raise
            # The message was deleted between the ack and the reaction (18.4).
            logger.info("telegram reaction target is gone", extra={"channel": "telegram"})
            return await self._send_degraded(identity, emoji, block)

        # `setMessageReaction` answers `true`, not a message. The receipt names
        # what was reacted to, marked as a reaction: `caps.edit_message` invites
        # a correction to rewrite the confirmation (13.2), and this one is the
        # *user's* message — which the bot does not own and Telegram refuses.
        return SendReceipt(
            channel=self.kind,
            channel_message_id=f"{REACTION_PREFIX}{message_id}",
            sent_at=self._now(),
        )

    async def _send_degraded(
        self, identity: ChannelIdentity, emoji: str, block: OutboundBlock
    ) -> SendReceipt:
        """The acknowledgement as a message, when it cannot be a reaction."""
        payload = self._text_payload(identity, emoji, block)
        return self._receipt(await self._client.call("sendMessage", payload))

    async def _send_media(self, identity: ChannelIdentity, block: OutboundBlock) -> SendReceipt:
        if block.media_path is None:
            raise ChannelError("a media block needs a file to send")
        data: dict[str, Any] = {"chat_id": identity.external_id}
        if block.reply_to is not None:
            # `ensure_addressable` already validated the target; dropping it
            # here would waste the check and deliver a chart that answers
            # nothing. Multipart carries it as JSON, like every other field.
            data["reply_parameters"] = json.dumps({"message_id": _addressable(block.reply_to[1])})
        if block.text:
            # Clip first, translate second. Telegram counts a caption after
            # entities are parsed, so the ceiling belongs on the visible text —
            # and slicing the HTML instead would cut through a tag and lose the
            # whole request to a parse error.
            data["caption"] = to_telegram_html(
                clip_caption(block.text, limit=self.caps.caption_limit)
            )
            data["parse_mode"] = "HTML"
        sent = await self._client.upload(
            "sendPhoto",
            data,
            {"photo": (block.media_path.name, block.media_path.read_bytes(), "image/png")},
        )
        return self._receipt(sent, media_ref=_photo_file_id(sent))

    def _text_payload(
        self, identity: ChannelIdentity, text: str, block: OutboundBlock
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "chat_id": identity.external_id,
            "text": to_telegram_html(text),
            "parse_mode": "HTML",
            # A preview turns an exercise name that looks like a domain into a
            # card, and the message stops reading as conversation.
            "link_preview_options": {"is_disabled": True},
        }
        if block.reply_to is not None and block.kind != "reaction":
            payload["reply_parameters"] = {"message_id": _addressable(block.reply_to[1])}
        return payload

    def _receipt(self, sent: Any, *, media_ref: str | None = None) -> SendReceipt:
        return SendReceipt(
            channel=self.kind,
            channel_message_id=str(sent["message_id"]),
            sent_at=self._now(),
            media_ref=media_ref,
        )


def _is_private(chat: Mapping[str, Any] | None) -> bool:
    """Whether this chat is one person talking to the bot.

    The bot operates in private chat only (spec 18.2). A group's `chat.id` is
    shared by everyone in it, so accepting a group update would file every
    member's messages under one tenant and send the answers — somebody else's
    training history — back to the whole room. Silence is the right answer to
    being added to a group.
    """
    return chat is not None and chat.get("type") == "private"


def _classify_message(
    message: Mapping[str, Any],
) -> tuple[InboundKind, str | None, str | None]:
    """What a `message` update is, of the kinds 18.2 handles."""
    if text := message.get("text"):
        return "text", str(text), None

    # Voice, and the two containers that are voice when they are short enough
    # (spec 11). Past the ceiling it is still recorded, but with no `media_ref`:
    # nothing downstream should fetch what will not be transcribed.
    #
    # `other` is the only truthful value the closed Literal of 18.1 offers, and
    # it is doing three jobs at once — this, a reaction update to discard, and a
    # membership change to act on. The ingress has to tell them apart from the
    # payload, because 11.3 asks the user to split the recording and 18.4 says
    # never silence. Noted for S02-T03; giving 18.1 a field instead is an ADR.
    for field in ("voice", "audio", "video_note"):
        if media := message.get(field):
            if int(media.get("duration", 0)) > MAX_AUDIO_SECONDS:
                return "other", None, None
            return "voice", None, str(media["file_id"])

    if photo := message.get("photo"):
        # Telegram sends a ladder of thumbnails; the last is the original.
        return "image", None, str(photo[-1]["file_id"])
    if document := message.get("document"):
        return "document", None, str(document["file_id"])
    return "other", None, None


def _addressable(channel_message_id: str) -> int:
    """A `channel_message_id` as the message number Telegram will accept.

    A press and a bare update are events, not messages: there is nothing to
    reply to and nothing to react to. Both are prefixed for exactly this check,
    so the category error fails here instead of addressing a stranger's message
    that happens to carry the same number.
    """
    if channel_message_id.startswith((PRESS_PREFIX, UPDATE_PREFIX, REACTION_PREFIX)):
        raise ChannelError(
            f"{channel_message_id!r} does not identify a message of ours: "
            "a press, an update and a reaction have nothing to reply to or rewrite"
        )
    try:
        return int(channel_message_id)
    except ValueError:
        raise ChannelError(f"{channel_message_id!r} is not a telegram message id") from None


def _photo_file_id(sent: Any) -> str | None:
    """The reusable id Telegram gives back, which makes a later retry free."""
    photo = sent.get("photo") if isinstance(sent, dict) else None
    return str(photo[-1]["file_id"]) if photo else None


def _retry_after(parameters: Mapping[str, Any]) -> int | None:
    """`parameters.retry_after`, when it is a number of seconds to wait.

    A malformed value is not believed: a string, a null or a negative would
    become a retry that fires immediately against a channel that just said no.
    """
    seconds = parameters.get("retry_after")
    if not isinstance(seconds, int) or isinstance(seconds, bool) or seconds < 0:
        return None
    return seconds
