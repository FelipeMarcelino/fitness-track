"""The Telegram adapter (spec 18.2, 13.2, 13.4; sprint S02-T02).

The adapter is the only place in the system that knows what a Telegram update
looks like, so these tests are mostly about the translation in both directions:
an update becomes an `InboundMessage`, an `OutboundBlock` becomes an API call.

Four things here are not translation and matter more:

1. **`verify` is constant time and comes before parsing.** The shared secret is
   the only thing standing between the ingress and anybody who knows the URL
   (spec 18.2).
2. **The token and the `file_path` never leave the module.** Both are on the
   redaction list of 20.2, and the `file_path` is the one that looks innocent —
   it reads as a path and carries the bot token inside it (spec 11.1).
3. **The reaction set is protocol knowledge.** Telegram takes one emoji from a
   fixed list, and the map in 13.2 says an emoji outside it degrades to text
   *in the adapter*, because the agent has no business knowing the list.
4. **`callback_data` is user input on the way back.** It carries an index and
   never content, so a client-controlled value cannot walk into the domain
   (spec 18.2).

No test here opens a socket: the client takes an `httpx.AsyncClient`, and the
tests hand it a `MockTransport`.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from pydantic import SecretStr

from fittrack.channels.base import (
    Channel,
    ChannelAuthenticationError,
    ChannelError,
    ChannelIdentity,
    ChannelMismatchError,
    OutboundBlock,
    TemplateRef,
)
from fittrack.channels.telegram.adapter import (
    MAX_AUDIO_SECONDS,
    TELEGRAM_REACTIONS,
    TelegramAdapter,
)
from fittrack.channels.telegram.client import (
    MAX_DOWNLOAD_BYTES,
    TelegramApiError,
    TelegramClient,
    TelegramTransportError,
)
from fittrack.channels.telegram.markup import (
    MAX_CALLBACK_DATA_BYTES,
    callback_data,
    clip_caption,
    to_telegram_html,
)
from fittrack.channels.telegram.secret import SECRET_HEADER

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

# A token shaped like the real thing. Every test that touches the network
# asserts this string is absent from whatever it inspects.
TOKEN = "8100000000:AAH-this-is-not-a-real-bot-token-000000"
SECRET = "s" * 43

IDENTITY = ChannelIdentity(identity_id=1, tenant_id=7, channel="telegram", external_id="987654321")
WHATSAPP_IDENTITY = ChannelIdentity(
    identity_id=2, tenant_id=7, channel="whatsapp", external_id="5511999999999"
)

FROZEN_NOW = datetime(2026, 3, 1, 12, 30, tzinfo=UTC)


class Recorder:
    """Answers Telegram calls from a script and remembers what was asked."""

    def __init__(self, replies: dict[str, Any] | None = None) -> None:
        self.replies = replies or {}
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.urls: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.urls.append(str(request.url))
        method = request.url.path.rsplit("/", 1)[-1]
        body = self._body(request)
        self.calls.append((method, body))
        reply = self.replies.get(method, {"message_id": 1, "date": 1772000000})
        if isinstance(reply, httpx.Response):
            return reply
        return httpx.Response(200, json={"ok": True, "result": reply})

    @staticmethod
    def _body(request: httpx.Request) -> dict[str, Any]:
        content = request.content
        if not content:
            return {}
        if request.headers.get("content-type", "").startswith("application/json"):
            parsed: dict[str, Any] = json.loads(content)
            return parsed
        return {"multipart": content}

    def payload(self, method: str) -> dict[str, Any]:
        for name, body in self.calls:
            if name == method:
                return body
        raise AssertionError(f"{method} was never called; saw {[n for n, _ in self.calls]}")

    @property
    def methods(self) -> list[str]:
        return [name for name, _ in self.calls]


def build_adapter(
    recorder: Recorder | None = None,
    *,
    secret: str | None = SECRET,
    download_dir: Path | None = None,
    now: Callable[[], datetime] = lambda: FROZEN_NOW,
) -> tuple[TelegramAdapter, Recorder]:
    recorder = recorder or Recorder()
    http = httpx.AsyncClient(transport=httpx.MockTransport(recorder.handler))
    client = TelegramClient(SecretStr(TOKEN), http=http)
    adapter = TelegramAdapter(
        client,
        webhook_secret=SecretStr(secret) if secret is not None else None,
        download_dir=download_dir,
        now=now,
    )
    return adapter, recorder


@pytest.fixture
def adapter() -> Iterator[TelegramAdapter]:
    built, _ = build_adapter()
    yield built


# --------------------------------------------------------------------------- #
# The adapter is a Channel
# --------------------------------------------------------------------------- #


def test_the_adapter_satisfies_the_channel_protocol(adapter: TelegramAdapter) -> None:
    held: Channel = adapter
    assert isinstance(held, Channel)
    assert held.kind == "telegram"


def test_the_capabilities_are_the_ones_section_18_1_lists(adapter: TelegramAdapter) -> None:
    caps = adapter.caps
    assert caps.reactions and caps.reaction_set == "restricted"
    assert caps.buttons and caps.max_buttons == 8
    assert caps.text_limit == 4096
    assert caps.caption_limit == 1024
    assert caps.typing_indicator and caps.edit_message and caps.delete_message
    assert caps.proactive == "free" and caps.window_hours is None
    assert caps.media_upload == "inline"
    assert caps.markup == "telegram_html"
    assert caps.max_bubbles == 3


# --------------------------------------------------------------------------- #
# verify — the shared secret, before anything is parsed
# --------------------------------------------------------------------------- #


def test_the_right_secret_is_accepted(adapter: TelegramAdapter) -> None:
    adapter.verify({SECRET_HEADER: SECRET}, b'{"update_id": 1}')


def test_the_header_is_matched_whatever_its_case(adapter: TelegramAdapter) -> None:
    """Header names are case-insensitive, and a dict is not."""
    adapter.verify({SECRET_HEADER.lower(): SECRET}, b"{}")
    adapter.verify({SECRET_HEADER.upper(): SECRET}, b"{}")


@pytest.mark.parametrize(
    ("headers", "why"),
    [
        pytest.param({}, "no header at all", id="absent"),
        pytest.param({SECRET_HEADER: ""}, "an empty value", id="empty"),
        pytest.param({SECRET_HEADER: "s" * 42}, "one character short", id="near miss"),
        pytest.param({SECRET_HEADER: "S" * 43}, "the wrong case", id="wrong case"),
        pytest.param({"X-Other": SECRET}, "the right value in the wrong header", id="wrong header"),
    ],
)
def test_a_wrong_secret_is_refused(
    adapter: TelegramAdapter, headers: dict[str, str], why: str
) -> None:
    with pytest.raises(ChannelAuthenticationError):
        adapter.verify(headers, b'{"update_id": 1}')


def test_the_refusal_does_not_quote_the_secret(adapter: TelegramAdapter) -> None:
    """The message says which header failed, never what either side held."""
    with pytest.raises(ChannelAuthenticationError) as raised:
        adapter.verify({SECRET_HEADER: "wrong-" + SECRET}, b"{}")
    printed = str(raised.value)
    assert SECRET not in printed
    assert "wrong-" not in printed


def test_an_adapter_without_a_secret_refuses_everything() -> None:
    """Polling has no webhook, so a webhook request to it is not authentic."""
    without, _ = build_adapter(secret=None)
    with pytest.raises(ChannelAuthenticationError):
        without.verify({SECRET_HEADER: SECRET}, b"{}")


def test_the_comparison_is_constant_time() -> None:
    """`hmac.compare_digest`, not `==`: the length of the shared prefix leaks."""
    import inspect

    from fittrack.channels.telegram import secret as module

    source = inspect.getsource(module)
    assert "compare_digest" in source


# --------------------------------------------------------------------------- #
# parse — every update type of the table in 18.2
# --------------------------------------------------------------------------- #

SENT_AT = datetime(2026, 2, 14, 10, 0, tzinfo=UTC)
EPOCH = int(SENT_AT.timestamp())


def message(**fields: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "message_id": 4242,
        "date": EPOCH,
        "chat": {"id": 987654321, "type": "private"},
        "from": {"id": 987654321, "is_bot": False},
    }
    return {"update_id": 10, "message": {**base, **fields}}


def test_a_text_message_becomes_one_inbound_message(adapter: TelegramAdapter) -> None:
    [parsed] = adapter.parse(message(text="supino reto 80kg x8"))
    assert parsed.channel == "telegram"
    assert parsed.kind == "text"
    assert parsed.text == "supino reto 80kg x8"
    assert parsed.external_id == "987654321"
    assert parsed.channel_message_id == "4242"
    assert parsed.sent_at == SENT_AT
    assert parsed.media_ref is None
    assert parsed.button_payload is None


def test_the_raw_update_travels_with_the_message(adapter: TelegramAdapter) -> None:
    """`raw_message` holds what arrived, so the audit is the update (invariant 6)."""
    payload = message(text="oi")
    [parsed] = adapter.parse(payload)
    assert parsed.raw["update_id"] == 10
    assert parsed.raw["message"]["text"] == "oi"


def test_a_voice_note_carries_its_file_id(adapter: TelegramAdapter) -> None:
    [parsed] = adapter.parse(message(voice={"file_id": "AwACAgE", "duration": 12}))
    assert parsed.kind == "voice"
    assert parsed.media_ref == "AwACAgE"
    assert parsed.text is None


@pytest.mark.parametrize("field", ["audio", "video_note"], ids=["audio", "video note"])
def test_audio_and_video_notes_are_voice_when_they_fit(
    adapter: TelegramAdapter, field: str
) -> None:
    """Section 11 caps the duration, not the container."""
    [parsed] = adapter.parse(
        message(**{field: {"file_id": "AwADBA", "duration": MAX_AUDIO_SECONDS}})
    )
    assert parsed.kind == "voice"
    assert parsed.media_ref == "AwADBA"


@pytest.mark.parametrize("field", ["audio", "video_note", "voice"])
def test_audio_past_the_limit_is_not_transcribed(adapter: TelegramAdapter, field: str) -> None:
    """Five minutes is the ceiling (11.3); past it the pipeline asks to split.

    It is still parsed and still recorded — `raw_message` keeps everything —
    but it carries no `media_ref`, so nothing downstream tries to fetch it.
    """
    [parsed] = adapter.parse(
        message(**{field: {"file_id": "AwADBA", "duration": MAX_AUDIO_SECONDS + 1}})
    )
    assert parsed.kind == "other"
    assert parsed.media_ref is None


def test_a_photo_takes_the_largest_size(adapter: TelegramAdapter) -> None:
    """Telegram sends a ladder of thumbnails; the last one is the original."""
    [parsed] = adapter.parse(
        message(
            photo=[
                {"file_id": "small", "width": 90, "height": 90},
                {"file_id": "large", "width": 1280, "height": 1280},
            ]
        )
    )
    assert parsed.kind == "image"
    assert parsed.media_ref == "large"


def test_a_document_is_a_document(adapter: TelegramAdapter) -> None:
    [parsed] = adapter.parse(message(document={"file_id": "BQACAgE", "file_name": "ficha.pdf"}))
    assert parsed.kind == "document"
    assert parsed.media_ref == "BQACAgE"


def test_a_button_press_carries_its_index_and_not_its_label(adapter: TelegramAdapter) -> None:
    """`callback_data` returns as user input, so it is an index and never content."""
    [parsed] = adapter.parse(
        {
            "update_id": 11,
            "callback_query": {
                "id": "3141592653",
                "from": {"id": 987654321},
                "data": "opt:2",
                "message": {
                    "message_id": 4242,
                    "date": EPOCH,
                    "chat": {"id": 987654321, "type": "private"},
                },
            },
        }
    )
    assert parsed.kind == "button_reply"
    assert parsed.button_payload == "opt:2"
    assert parsed.external_id == "987654321"
    assert parsed.text is None


def test_a_button_press_is_identified_by_the_press(adapter: TelegramAdapter) -> None:
    """Two presses on one message are two events, and both have to be recordable.

    `raw_message` is unique on `(identity_id, channel_message_id)` (17.4). Keying
    a press by the message it sits under would make the second press collide
    with the first and vanish.
    """
    press = {
        "update_id": 11,
        "callback_query": {
            "id": "3141592653",
            "from": {"id": 987654321},
            "data": "opt:2",
            "message": {
                "message_id": 4242,
                "date": EPOCH,
                "chat": {"id": 987654321, "type": "private"},
            },
        },
    }
    [parsed] = adapter.parse(press)
    assert parsed.channel_message_id == "3141592653"
    assert parsed.sent_at == FROZEN_NOW, "a callback query has no date of its own"


def test_a_reaction_update_is_recorded_and_not_processed(adapter: TelegramAdapter) -> None:
    """18.2 ignores it. It is still parsed, so `raw_message` has the audit row,
    and the ingress drops it before the buffer (S02-T03) rather than the adapter
    deciding what deserves to exist.
    """
    [parsed] = adapter.parse(
        {
            "update_id": 12,
            "message_reaction": {
                "chat": {"id": 987654321},
                "message_id": 4242,
                "user": {"id": 987654321},
                "date": EPOCH,
                "new_reaction": [{"type": "emoji", "emoji": "👍"}],
            },
        }
    )
    assert parsed.kind == "other"


def test_two_reactions_to_one_message_are_two_recordable_events(
    adapter: TelegramAdapter,
) -> None:
    """`raw_message` is unique on `(identity_id, channel_message_id)` (17.4).

    A reaction update names the message it reacted to, so keying by that would
    make a second reaction collide with the first and fail the ingress on an
    update 18.2 does not even process. The update id is what is unique per
    event.
    """

    def reaction(update_id: int, emoji: str) -> dict[str, Any]:
        return {
            "update_id": update_id,
            "message_reaction": {
                "chat": {"id": 987654321},
                "message_id": 4242,
                "user": {"id": 987654321},
                "date": EPOCH,
                "new_reaction": [{"type": "emoji", "emoji": emoji}],
            },
        }

    [first] = adapter.parse(reaction(20, "👍"))
    [second] = adapter.parse(reaction(21, "🔥"))
    assert first.channel_message_id != second.channel_message_id


def test_a_block_is_recorded_so_the_identity_can_be_revoked(adapter: TelegramAdapter) -> None:
    """`my_chat_member` kicked/blocked marks `revoked_at` (18.2) — at the ingress.

    The adapter writes nothing; it makes the event visible with its payload
    intact so the layer that owns identity can act on it.
    """
    [parsed] = adapter.parse(
        {
            "update_id": 13,
            "my_chat_member": {
                "chat": {"id": 987654321, "type": "private"},
                "from": {"id": 987654321},
                "date": EPOCH,
                "old_chat_member": {"status": "member"},
                "new_chat_member": {"status": "kicked"},
            },
        }
    )
    assert parsed.kind == "other"
    assert parsed.raw["my_chat_member"]["new_chat_member"]["status"] == "kicked"


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param({"update_id": 14}, id="nothing but an id"),
        pytest.param({"update_id": 15, "edited_message": {"message_id": 1}}, id="an edit"),
        pytest.param({"update_id": 16, "poll": {"id": "1"}}, id="a poll"),
        pytest.param({"update_id": 17, "inline_query": {"id": "1"}}, id="an inline query"),
    ],
)
def test_an_update_we_never_asked_for_is_ignored_silently(
    adapter: TelegramAdapter, payload: dict[str, Any]
) -> None:
    """`allowed_updates` should keep these out; parsing survives them anyway."""
    assert adapter.parse(payload) == []


def test_a_message_with_nothing_we_handle_is_still_recorded(adapter: TelegramAdapter) -> None:
    """A sticker is a message. It becomes `other`, not an exception."""
    [parsed] = adapter.parse(message(sticker={"file_id": "CAACAgE"}))
    assert parsed.kind == "other"
    assert parsed.text is None


# --------------------------------------------------------------------------- #
# markup — the neutral dialect becomes Telegram HTML (13.4)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("neutral", "html"),
    [
        pytest.param("**80 kg**", "<b>80 kg</b>", id="bold"),
        pytest.param("__leve__", "<i>leve</i>", id="italic"),
        pytest.param("`supino_reto_barra`", "<code>supino_reto_barra</code>", id="mono"),
        pytest.param("~~75 kg~~", "<s>75 kg</s>", id="struck"),
        pytest.param("**a** e __b__", "<b>a</b> e <i>b</i>", id="two of them"),
        pytest.param("sem marcação", "sem marcação", id="none at all"),
    ],
)
def test_the_neutral_markup_becomes_telegram_html(neutral: str, html: str) -> None:
    """The agent emits `**b**`; the adapter translates (13.4).

    Making the LLM write Telegram HTML would add a second failure mode: malformed
    HTML is a 400, and the model is the wrong place to be careful about that.
    """
    assert to_telegram_html(neutral) == html


@pytest.mark.parametrize(
    ("raw", "escaped"),
    [
        pytest.param("supino < inclinado", "supino &lt; inclinado", id="a stray less-than"),
        pytest.param("a & b", "a &amp; b", id="an ampersand"),
        pytest.param(
            "<script>alert(1)</script>", "&lt;script&gt;alert(1)&lt;/script&gt;", id="a tag"
        ),
        pytest.param("2 > 1", "2 &gt; 1", id="a greater-than"),
    ],
)
def test_everything_else_is_escaped_before_it_can_be_markup(raw: str, escaped: str) -> None:
    """A `<` in an exercise name would otherwise be a 400 on send (13.4)."""
    assert to_telegram_html(raw) == escaped


def test_escaping_happens_before_translation_not_after() -> None:
    """Otherwise the tags this function just wrote would be escaped as well."""
    assert to_telegram_html("**<b>já em html</b>**") == "<b>&lt;b&gt;já em html&lt;/b&gt;</b>"


def test_the_ampersand_of_an_entity_is_escaped_once() -> None:
    """`&amp;` twice over renders as `&amp;` on screen, not as `&`."""
    assert to_telegram_html("&lt;") == "&amp;lt;"


def test_callback_data_carries_an_index_and_nothing_else() -> None:
    """A `callback_data` with an `exercise_id` is a client-controlled parameter
    walking into the domain (18.2). It carries the option's position instead.
    """
    assert callback_data(0) == "opt:0"
    assert callback_data(7) == "opt:7"


@pytest.mark.parametrize("index", [0, 7, 99, 999999])
def test_callback_data_fits_the_sixty_four_byte_ceiling(index: int) -> None:
    assert len(callback_data(index).encode()) <= MAX_CALLBACK_DATA_BYTES


def test_a_caption_is_clipped_to_what_the_api_takes() -> None:
    clipped = clip_caption("x" * 2000, limit=1024)
    assert len(clipped) == 1024


def test_a_caption_that_fits_is_left_alone() -> None:
    assert clip_caption("Supino reto — 12 semanas", limit=1024) == "Supino reto — 12 semanas"


# --------------------------------------------------------------------------- #
# send — one block, one call, guard first
# --------------------------------------------------------------------------- #


async def test_a_text_block_is_sent_as_html_without_a_preview() -> None:
    adapter, recorder = build_adapter(Recorder({"sendMessage": {"message_id": 77, "date": EPOCH}}))
    receipt = await adapter.send(IDENTITY, OutboundBlock(kind="text", text="**80 kg** anotado"))

    body = recorder.payload("sendMessage")
    assert body["chat_id"] == "987654321"
    assert body["text"] == "<b>80 kg</b> anotado"
    assert body["parse_mode"] == "HTML"
    assert body["link_preview_options"] == {"is_disabled": True}
    assert receipt.channel == "telegram"
    assert receipt.channel_message_id == "77"


async def test_a_reply_names_the_message_it_answers() -> None:
    adapter, recorder = build_adapter(Recorder({"sendMessage": {"message_id": 78, "date": EPOCH}}))
    await adapter.send(
        IDENTITY, OutboundBlock(kind="text", text="oi", reply_to=("telegram", "4242"))
    )
    assert recorder.payload("sendMessage")["reply_parameters"] == {"message_id": 4242}


async def test_the_guard_runs_before_any_request() -> None:
    """`ensure_addressable` first, so a mismatch never reaches the network."""
    adapter, recorder = build_adapter()
    with pytest.raises(ChannelMismatchError):
        await adapter.send(WHATSAPP_IDENTITY, OutboundBlock(kind="text", text="oi"))
    assert recorder.calls == []


async def test_a_reply_to_another_channel_never_reaches_the_network() -> None:
    adapter, recorder = build_adapter()
    with pytest.raises(ChannelMismatchError):
        await adapter.send(
            IDENTITY, OutboundBlock(kind="text", text="oi", reply_to=("whatsapp", "wamid.X"))
        )
    assert recorder.calls == []


async def test_a_reaction_uses_set_message_reaction() -> None:
    adapter, recorder = build_adapter(Recorder({"setMessageReaction": True}))
    receipt = await adapter.send(
        IDENTITY, OutboundBlock(kind="reaction", emoji="👍", reply_to=("telegram", "4242"))
    )
    body = recorder.payload("setMessageReaction")
    assert body["chat_id"] == "987654321"
    assert body["message_id"] == 4242
    assert body["reaction"] == [{"type": "emoji", "emoji": "👍"}]
    assert receipt.channel_message_id == "4242", "the receipt points at the message reacted to"


def test_the_reaction_set_is_the_one_telegram_publishes() -> None:
    """13.2 is explicit that `✅` is not in it — which is why the map exists."""
    assert "👍" in TELEGRAM_REACTIONS
    assert "🔥" in TELEGRAM_REACTIONS
    assert "🏆" in TELEGRAM_REACTIONS
    assert "✅" not in TELEGRAM_REACTIONS


async def test_an_emoji_outside_the_set_degrades_to_text_here() -> None:
    """13.2 puts the degradation in the adapter: it is protocol knowledge.

    The agent chose an acknowledgement; turning it into a 400 would lose it, and
    teaching the agent Telegram's emoji list would put protocol in the domain.
    """
    adapter, recorder = build_adapter(Recorder({"sendMessage": {"message_id": 79, "date": EPOCH}}))
    receipt = await adapter.send(
        IDENTITY, OutboundBlock(kind="reaction", emoji="✅", reply_to=("telegram", "4242"))
    )
    assert "setMessageReaction" not in recorder.methods
    assert recorder.payload("sendMessage")["text"] == "✅"
    assert receipt.channel_message_id == "79"


async def test_buttons_become_an_inline_keyboard() -> None:
    adapter, recorder = build_adapter(Recorder({"sendMessage": {"message_id": 80, "date": EPOCH}}))
    await adapter.send(
        IDENTITY,
        OutboundBlock(
            kind="buttons",
            text="Foi reto ou inclinado?",
            buttons=("supino reto", "supino inclinado"),
        ),
    )
    body = recorder.payload("sendMessage")
    assert body["reply_markup"] == {
        "inline_keyboard": [
            [{"text": "supino reto", "callback_data": "opt:0"}],
            [{"text": "supino inclinado", "callback_data": "opt:1"}],
        ]
    }


async def test_more_buttons_than_the_channel_takes_is_a_bug_not_a_400() -> None:
    """`caps.max_buttons` is 8; the voice agent degrades to a numbered list
    above that (9.10), so arriving here with nine is our bug, caught before the
    request rather than after it.
    """
    adapter, recorder = build_adapter()
    with pytest.raises(ChannelError, match="max_buttons"):
        await adapter.send(
            IDENTITY,
            OutboundBlock(kind="buttons", text="?", buttons=tuple(f"o{i}" for i in range(9))),
        )
    assert recorder.calls == []


async def test_media_goes_up_in_one_request_with_its_caption(tmp_path: Path) -> None:
    """Telegram is `media_upload="inline"`: the bytes ride with the message."""
    chart = tmp_path / "progress.png"
    chart.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 32)
    adapter, recorder = build_adapter(
        Recorder({"sendPhoto": {"message_id": 81, "date": EPOCH, "photo": [{"file_id": "AgACX"}]}})
    )
    receipt = await adapter.send(
        IDENTITY,
        OutboundBlock(kind="media", text="Supino reto — 12 semanas", media_path=chart),
    )
    assert "sendPhoto" in recorder.methods
    assert receipt.media_ref == "AgACX", "the file_id makes a later retry free (18.2)"
    assert receipt.channel_message_id == "81"


async def test_a_caption_longer_than_the_api_takes_is_clipped(tmp_path: Path) -> None:
    chart = tmp_path / "progress.png"
    chart.write_bytes(b"\x89PNG")
    adapter, recorder = build_adapter(
        Recorder({"sendPhoto": {"message_id": 82, "date": EPOCH, "photo": [{"file_id": "A"}]}})
    )
    await adapter.send(IDENTITY, OutboundBlock(kind="media", text="c" * 3000, media_path=chart))
    body = recorder.payload("sendPhoto")["multipart"]
    assert b"c" * 1024 in body
    assert b"c" * 1025 not in body


async def test_a_template_block_is_refused() -> None:
    """Templates are the windowed channels' problem; Telegram has no such thing."""
    adapter, recorder = build_adapter()
    with pytest.raises(ChannelError, match="template"):
        await adapter.send(
            IDENTITY,
            OutboundBlock(kind="template", template=TemplateRef(name="retomada", language="pt_BR")),
        )
    assert recorder.calls == []


async def test_typing_is_an_action_and_not_a_bubble() -> None:
    """`sendChatAction` expires in about five seconds, so `deliver` repeats it."""
    adapter, recorder = build_adapter(Recorder({"sendChatAction": True}))
    await adapter.send_typing(IDENTITY)
    assert recorder.payload("sendChatAction") == {"chat_id": "987654321", "action": "typing"}


# --------------------------------------------------------------------------- #
# download_media — two calls, a tmpfs file, and a path nobody may see
# --------------------------------------------------------------------------- #

FILE_PATH = "voice/file_123.oga"
OGG = b"OggS" + b"\x00" * 128


def downloading(payload: bytes = OGG, *, file_path: str = FILE_PATH) -> Recorder:
    recorder = Recorder({"getFile": {"file_id": "AwACAgE", "file_path": file_path}})
    inner = recorder.handler

    def handler(request: httpx.Request) -> httpx.Response:
        if "/file/bot" in request.url.path:
            recorder.urls.append(str(request.url))
            recorder.calls.append(("download", {}))
            return httpx.Response(200, content=payload)
        return inner(request)

    recorder.handler = handler  # type: ignore[method-assign]
    return recorder


async def test_media_is_fetched_in_two_calls_and_lands_in_tmpfs(tmp_path: Path) -> None:
    """`getFile` gives the path, the second request gives the bytes (11.1)."""
    adapter, recorder = build_adapter(downloading(), download_dir=tmp_path)
    where = await adapter.download_media("AwACAgE")

    assert recorder.methods == ["getFile", "download"]
    assert recorder.payload("getFile") == {"file_id": "AwACAgE"}
    assert where.parent == tmp_path
    assert where.read_bytes() == OGG


async def test_the_downloaded_name_does_not_echo_the_secret_path(tmp_path: Path) -> None:
    """The file lands under a name of ours: `file_path` is the access secret,
    and writing it into a filename puts it in every traceback that lists a path.
    """
    adapter, _ = build_adapter(downloading(), download_dir=tmp_path)
    where = await adapter.download_media("AwACAgE")
    assert "file_123" not in where.name
    assert where.suffix == ".oga", "the extension is worth keeping; the path is not"


async def test_a_download_over_the_ceiling_is_refused(tmp_path: Path) -> None:
    """20 MB is the public API's ceiling (18.2); past it we stop reading."""
    oversized = downloading(b"0" * (MAX_DOWNLOAD_BYTES + 1))
    adapter, _ = build_adapter(oversized, download_dir=tmp_path)
    with pytest.raises(TelegramTransportError, match="ceiling"):
        await adapter.download_media("AwACAgE")
    assert list(tmp_path.iterdir()) == [], "a refused download leaves nothing behind"


async def test_a_file_id_that_telegram_does_not_know_fails_as_an_api_error(
    tmp_path: Path,
) -> None:
    recorder = Recorder(
        {
            "getFile": httpx.Response(
                400,
                json={"ok": False, "error_code": 400, "description": "Bad Request: file not found"},
            )
        }
    )
    adapter, _ = build_adapter(recorder, download_dir=tmp_path)
    with pytest.raises(TelegramApiError):
        await adapter.download_media("nope")


# --------------------------------------------------------------------------- #
# Redaction — the token and the file_path (20.2, 20.6, invariant 10)
# --------------------------------------------------------------------------- #


async def test_no_request_url_reaches_an_exception_with_the_token(tmp_path: Path) -> None:
    """httpx puts the URL in its own error messages, and the URL *is* the token.

    This is the leak that writes itself: one unhandled `HTTPStatusError` and the
    bot token is in the log, the trace and the incident ticket.
    """

    def explode(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    http = httpx.AsyncClient(transport=httpx.MockTransport(explode))
    client = TelegramClient(SecretStr(TOKEN), http=http)
    adapter = TelegramAdapter(client, webhook_secret=SecretStr(SECRET), download_dir=tmp_path)

    with pytest.raises(Exception) as raised:
        await adapter.send(IDENTITY, OutboundBlock(kind="text", text="oi"))
    assert TOKEN not in str(raised.value)
    assert TOKEN not in repr(raised.value)


async def test_an_api_error_does_not_carry_the_token(tmp_path: Path) -> None:
    recorder = Recorder(
        {
            "sendMessage": httpx.Response(
                400,
                json={"ok": False, "error_code": 400, "description": "Bad Request: chat not found"},
            )
        }
    )
    adapter, _ = build_adapter(recorder, download_dir=tmp_path)
    with pytest.raises(TelegramApiError) as raised:
        await adapter.send(IDENTITY, OutboundBlock(kind="text", text="oi"))
    assert TOKEN not in str(raised.value)
    assert TOKEN not in repr(raised.value)
    assert "chat not found" in str(raised.value), "the description is what we need to classify"


async def test_the_file_path_never_reaches_a_log_or_an_exception(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """It reads as a path and carries the bot token in the URL (11.1, 20.6)."""
    import logging

    caplog.set_level(logging.DEBUG)
    adapter, _ = build_adapter(downloading(), download_dir=tmp_path)
    await adapter.download_media("AwACAgE")

    printed = caplog.text
    assert FILE_PATH not in printed
    assert "file_123" not in printed
    assert TOKEN not in printed


async def test_a_failed_download_does_not_log_the_path_either(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    import logging

    caplog.set_level(logging.DEBUG)
    recorder = downloading(b"0" * (MAX_DOWNLOAD_BYTES + 1))
    adapter, _ = build_adapter(recorder, download_dir=tmp_path)
    with pytest.raises(Exception) as raised:
        await adapter.download_media("AwACAgE")
    assert FILE_PATH not in str(raised.value)
    assert FILE_PATH not in caplog.text
    assert TOKEN not in caplog.text


def test_the_adapter_does_not_repr_its_token(adapter: TelegramAdapter) -> None:
    assert TOKEN not in repr(adapter)
    assert SECRET not in repr(adapter)


# --------------------------------------------------------------------------- #
# The leak nobody writes: httpx logs the URL, and the URL is the token
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "line",
    [
        pytest.param(
            f'HTTP Request: POST https://api.telegram.org/bot{TOKEN}/sendMessage "HTTP/1.1 200 OK"',
            id="a method call",
        ),
        pytest.param(
            f"HTTP Request: GET https://api.telegram.org/file/bot{TOKEN}/{FILE_PATH}",
            id="a media download",
        ),
    ],
)
def test_a_telegram_url_is_redacted_wherever_it_is_logged(line: str) -> None:
    """`httpx` logs every request at INFO with its URL — which for Telegram is
    the bot token on every call and the `file_path` on every download. Nobody
    wrote that log line, so nobody reviews it: it is invariant 10 by accident.
    """
    from fittrack.channels.telegram.client import redact_telegram_urls

    redacted = redact_telegram_urls(line)
    assert TOKEN not in redacted
    assert "file_123" not in redacted
    assert "<redacted>" in redacted
    assert "HTTP Request" in redacted, "the line is still useful"


def test_redaction_leaves_other_hosts_alone() -> None:
    """The filter is on the shared `httpx` logger: the provider SDKs use it too."""
    from fittrack.channels.telegram.client import redact_telegram_urls

    line = 'HTTP Request: POST https://api.groq.com/openai/v1/chat "HTTP/1.1 200 OK"'
    assert redact_telegram_urls(line) == line


async def test_the_filter_is_installed_before_the_first_request(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Installed by the client's constructor, because the first call is enough."""
    import logging

    caplog.set_level(logging.INFO)
    adapter, _ = build_adapter(downloading(), download_dir=tmp_path)
    await adapter.download_media("AwACAgE")

    assert "api.telegram.org" in caplog.text, "httpx still logs the request"
    assert TOKEN not in caplog.text
    assert FILE_PATH not in caplog.text
