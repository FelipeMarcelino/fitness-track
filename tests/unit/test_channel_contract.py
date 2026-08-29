"""The contract every channel implements (spec 18.1, 18.4; sprint S02-T01).

Three things are worth a test here, and they are not the dataclasses.

1. **`reply_to` carries its channel.** A tenant with Telegram and WhatsApp
   linked has two message id spaces, and nothing but this check stops the
   system from reacting to a Telegram message with a WhatsApp id. The rejection
   has to happen before the request leaves, which is why the fake adapter below
   records what it would have sent instead of sending it.
2. **The registry builds what `FITTRACK_CHANNELS` lists, and nothing else.** A
   channel with a missing credential fails at build time, by name, rather than
   handing out a half-built adapter that fails on the first webhook.
3. **The concrete adapters stay inside `channels/`.** `test_channel_isolation`
   already forbids `graph/` and `agents/` from importing the package at all;
   this covers the rest of `src/`, which may hold a `Channel` but never a
   `TelegramAdapter`.
"""

from __future__ import annotations

import ast
import dataclasses
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

import pytest

from fittrack.channels.base import (
    Channel,
    ChannelAuthenticationError,
    ChannelCaps,
    ChannelIdentity,
    ChannelMismatchError,
    ClassifiedError,
    ErrorClass,
    InboundMessage,
    OutboundBlock,
    SendReceipt,
    TemplateRef,
    ensure_addressable,
)
from fittrack.channels.registry import (
    ADAPTERS,
    ChannelConfig,
    ChannelFactory,
    ChannelNotEnabledError,
    ChannelRegistry,
    ChannelUnavailableError,
    MissingCredentialError,
)
from fittrack.settings import KNOWN_CHANNELS, ChannelKind, TelegramMode

if TYPE_CHECKING:  # pragma: no cover - mypy is the assertion
    from pydantic import SecretStr

    from fittrack.settings import Settings

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"

CAPS = ChannelCaps(
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


class FakeChannel:
    """A conforming adapter that records instead of speaking a protocol."""

    kind: ClassVar[ChannelKind] = "telegram"
    caps: ClassVar[ChannelCaps] = CAPS

    def __init__(self) -> None:
        self.sent: list[OutboundBlock] = []

    def verify(self, headers: Mapping[str, str], raw_body: bytes) -> None:
        if headers.get("X-Secret") != "right":
            raise ChannelAuthenticationError("wrong secret")

    def parse(self, payload: Mapping[str, Any]) -> list[InboundMessage]:
        return []

    async def download_media(self, media_ref: str) -> Path:
        return Path("/tmp/media")

    async def send(self, identity: ChannelIdentity, block: OutboundBlock) -> SendReceipt:
        # The order is the point: the guard runs before anything is recorded,
        # exactly as it will run before anything is posted.
        ensure_addressable(self.kind, identity, block)
        self.sent.append(block)
        return SendReceipt(channel=self.kind, channel_message_id="1", sent_at=datetime.now(UTC))

    def classify_error(self, exc: Exception) -> ClassifiedError:
        return ClassifiedError(ErrorClass.BUG)


TELEGRAM_IDENTITY = ChannelIdentity(
    identity_id=1, tenant_id=7, channel="telegram", external_id="123456789"
)
WHATSAPP_IDENTITY = ChannelIdentity(
    identity_id=2, tenant_id=7, channel="whatsapp", external_id="5511999999999"
)


# --------------------------------------------------------------------------- #
# The types that cross the boundary
# --------------------------------------------------------------------------- #


def test_error_class_has_the_six_values_of_section_18_4() -> None:
    assert {member.value for member in ErrorClass} == {
        "retry_backoff",
        "retry_after",
        "defer_window",
        "undeliverable",
        "account",
        "bug",
    }


def test_error_class_is_a_string() -> None:
    """It is persisted in `outbound_queue.error_code` and compared as text."""
    persisted: str = "retry_after"
    assert persisted == ErrorClass.RETRY_AFTER
    assert f"{ErrorClass.ACCOUNT}" == "account"


def test_channel_caps_carries_every_capability_of_section_18_1() -> None:
    assert {f.name for f in dataclasses.fields(ChannelCaps)} == {
        "reactions",
        "reaction_set",
        "buttons",
        "max_buttons",
        "text_limit",
        "caption_limit",
        "typing_indicator",
        "edit_message",
        "delete_message",
        "proactive",
        "window_hours",
        "media_upload",
        "markup",
        "max_bubbles",
    }


def test_a_free_channel_has_no_window() -> None:
    """`window_hours` is None when proactive is free — the spec says so inline."""
    with pytest.raises(ValueError, match="window_hours"):
        dataclasses.replace(CAPS, proactive="free", window_hours=24)
    with pytest.raises(ValueError, match="window_hours"):
        dataclasses.replace(CAPS, proactive="windowed", window_hours=None)


def test_a_channel_without_reactions_has_no_reaction_set() -> None:
    with pytest.raises(ValueError, match="reaction_set"):
        dataclasses.replace(CAPS, reactions=False, reaction_set="arbitrary")


def test_a_channel_with_reactions_names_its_set() -> None:
    """A descriptor that supports reactions says *which* ones (spec 18.1).

    Telegram takes one emoji from a fixed list and WhatsApp takes any, and the
    ack map of 13.2 has a table per channel. `reactions=True` without the set
    tells the formatter it may react while telling it nothing about what with —
    so it picks an emoji the channel rejects, at send time.
    """
    with pytest.raises(ValueError, match="reaction_set"):
        dataclasses.replace(CAPS, reactions=True, reaction_set=None)


@pytest.mark.parametrize(
    "value",
    [
        CAPS,
        InboundMessage(
            channel="telegram",
            external_id="1",
            channel_message_id="2",
            kind="text",
            text="supino 80kg x8",
            media_ref=None,
            button_payload=None,
            sent_at=datetime.now(UTC),
            raw={},
        ),
        OutboundBlock(kind="text", text="ok"),
        TELEGRAM_IDENTITY,
        SendReceipt(channel="telegram", channel_message_id="9", sent_at=datetime.now(UTC)),
        TemplateRef(name="retomada_treino", language="pt_BR", parameters=("Felipe", "8")),
    ],
    ids=lambda value: type(value).__name__,
)
def test_the_boundary_types_are_frozen(value: object) -> None:
    """Shared with the queue, the buffer and the graph — nobody mutates them."""
    with pytest.raises(dataclasses.FrozenInstanceError):
        value.__setattr__("kind", "other")


def test_an_identity_never_reprs_its_external_id() -> None:
    """`external_id` is opaque and encrypted at rest; a repr reaches logs."""
    assert "123456789" not in repr(TELEGRAM_IDENTITY)
    assert "identity_id=1" in repr(TELEGRAM_IDENTITY)


def inbound_message(**overrides: Any) -> InboundMessage:
    """A text update as an adapter would hand it over, payload and all."""
    base: dict[str, Any] = {
        "channel": "telegram",
        "external_id": "987654321",
        "channel_message_id": "2",
        "kind": "text",
        "text": "supino 80kg x8",
        "media_ref": None,
        "button_payload": None,
        "sent_at": datetime.now(UTC),
        "raw": {"message": {"chat": {"id": 987654321}, "text": "supino 80kg x8"}},
    }
    return InboundMessage(**{**base, **overrides})


def test_an_inbound_message_never_reprs_what_the_user_wrote() -> None:
    """`channel.payload` and `user.text` are both redacted at the OTel processor
    (20.2), and the raw update carries the `chat.id` as well — so a repr walks
    the external id straight past the `repr=False` that guards the field itself.
    One logged exception with this object in the frame is the whole leak.
    """
    printed = repr(inbound_message())
    assert "987654321" not in printed
    assert "supino 80kg x8" not in printed
    assert "channel_message_id='2'" in printed, "the safe fields still identify it"


def test_the_raw_payload_is_a_snapshot_of_the_authenticated_update() -> None:
    """What lands in `raw_message` is what arrived, not what was left behind.

    An adapter ordinarily keeps the dict it parsed. Annotating the field as a
    `Mapping` does not copy it: without the snapshot, an edit anywhere in that
    tree rewrites an audit record that is supposed to be the received bytes
    (invariant 6).
    """
    payload: dict[str, Any] = {"message": {"chat": {"id": 987654321}, "text": "supino 80kg x8"}}
    message = inbound_message(raw=payload)

    payload["message"]["text"] = "agachamento 200kg"
    payload["message"]["chat"]["id"] = 111
    payload["edited"] = True

    assert message.raw["message"]["text"] == "supino 80kg x8"
    assert message.raw["message"]["chat"]["id"] == 987654321
    assert "edited" not in message.raw


def test_an_outbound_block_needs_only_its_kind() -> None:
    block = OutboundBlock(kind="reaction", emoji="👍")
    assert block.text is None
    assert block.buttons is None
    assert block.reply_to is None
    assert block.template is None


def test_reply_to_is_a_channel_and_a_message_id() -> None:
    block = OutboundBlock(kind="reaction", emoji="👍", reply_to=("telegram", "42"))
    assert block.reply_to == ("telegram", "42")


# --------------------------------------------------------------------------- #
# The protocol
# --------------------------------------------------------------------------- #


def test_a_conforming_adapter_satisfies_the_protocol() -> None:
    adapter: Channel = FakeChannel()
    assert isinstance(adapter, Channel)
    assert adapter.kind == "telegram"
    assert adapter.caps.max_buttons == 8


def test_an_adapter_missing_a_method_does_not_satisfy_the_protocol() -> None:
    class Partial:
        kind: ClassVar[ChannelKind] = "telegram"
        caps: ClassVar[ChannelCaps] = CAPS

        def verify(self, headers: Mapping[str, str], raw_body: bytes) -> None: ...

    assert not isinstance(Partial(), Channel)


def test_verify_raises_the_shared_authentication_error() -> None:
    """The ingress answers 403 without importing a concrete adapter (S02-T03)."""
    adapter = FakeChannel()
    adapter.verify({"X-Secret": "right"}, b"{}")
    with pytest.raises(ChannelAuthenticationError):
        adapter.verify({"X-Secret": "wrong"}, b"{}")


# --------------------------------------------------------------------------- #
# Channel drift, rejected before the request
# --------------------------------------------------------------------------- #


async def test_send_rejects_a_reply_to_from_another_channel() -> None:
    adapter = FakeChannel()
    block = OutboundBlock(kind="reaction", emoji="👍", reply_to=("whatsapp", "42"))
    with pytest.raises(ChannelMismatchError, match="whatsapp"):
        await adapter.send(TELEGRAM_IDENTITY, block)
    assert adapter.sent == [], "the block reached the wire before the check"


async def test_send_rejects_an_identity_from_another_channel() -> None:
    adapter = FakeChannel()
    with pytest.raises(ChannelMismatchError, match="whatsapp"):
        await adapter.send(WHATSAPP_IDENTITY, OutboundBlock(kind="text", text="oi"))
    assert adapter.sent == []


async def test_send_accepts_its_own_channel() -> None:
    adapter = FakeChannel()
    block = OutboundBlock(kind="reaction", emoji="👍", reply_to=("telegram", "42"))
    receipt = await adapter.send(TELEGRAM_IDENTITY, block)
    assert adapter.sent == [block]
    assert receipt.channel == "telegram"


def test_a_block_without_reply_to_is_addressable() -> None:
    ensure_addressable("telegram", TELEGRAM_IDENTITY, OutboundBlock(kind="text", text="oi"))


# --------------------------------------------------------------------------- #
# Error classification
# --------------------------------------------------------------------------- #


def test_a_classified_error_carries_the_channel_number() -> None:
    """`retry_after` travels with the class so outbound never parses a channel."""
    verdict = ClassifiedError(ErrorClass.RETRY_AFTER, retry_after=17, code="429")
    assert verdict.error_class is ErrorClass.RETRY_AFTER
    assert verdict.retry_after == 17
    assert verdict.code == "429"


def test_a_classified_error_defaults_to_no_number() -> None:
    verdict = ClassifiedError(ErrorClass.BUG)
    assert verdict.retry_after is None
    assert verdict.code is None


def test_only_retry_after_carries_a_delay() -> None:
    with pytest.raises(ValueError, match="retry_after"):
        ClassifiedError(ErrorClass.RETRY_BACKOFF, retry_after=17)


def test_retry_after_without_the_number_is_not_retry_after() -> None:
    """The class means "the channel said when" — the number is the whole point.

    The ladder belongs to `RETRY_BACKOFF`; `RETRY_AFTER` waits exactly what the
    429 carried (spec 18.4). An adapter that classifies without the value hands
    the outbound service a verdict it cannot write `next_retry_at` from, and the
    discovery happens in the worker rather than here.
    """
    with pytest.raises(ValueError, match="retry_after"):
        ClassifiedError(ErrorClass.RETRY_AFTER)


# --------------------------------------------------------------------------- #
# The registry
# --------------------------------------------------------------------------- #


@dataclasses.dataclass(frozen=True)
class StubSecret:
    """Enough of `SecretStr` for the registry, which only checks presence."""

    value: str

    def get_secret_value(self) -> str:
        return self.value


@dataclasses.dataclass(frozen=True)
class StubConfig:
    """A configuration the registry accepts, built field by field in a test.

    `Settings` is frozen and validates the same rules at boot, so it cannot be
    made to describe a channel with a missing credential. The registry has to
    be provable on its own.
    """

    channels: tuple[ChannelKind, ...] = ()
    telegram_bot_token: Any = None
    telegram_mode: TelegramMode = "webhook"
    telegram_webhook_secret: Any = None
    waba_phone_number_id: Any = None
    waba_token: Any = None
    waba_app_secret: Any = None
    waba_verify_token: Any = None


def telegram_config(**overrides: Any) -> StubConfig:
    base: dict[str, Any] = {
        "channels": ("telegram",),
        "telegram_bot_token": StubSecret("token"),
        "telegram_mode": "webhook",
        "telegram_webhook_secret": StubSecret("s" * 43),
    }
    return StubConfig(**{**base, **overrides})


def whatsapp_config(**overrides: Any) -> StubConfig:
    base: dict[str, Any] = {
        "channels": ("whatsapp",),
        "waba_phone_number_id": StubSecret("id"),
        "waba_token": StubSecret("token"),
        "waba_app_secret": StubSecret("secret"),
        "waba_verify_token": StubSecret("verify"),
    }
    return StubConfig(**{**base, **overrides})


class RecordingFactories:
    """Stand-ins for the shipped adapters, remembering what was built.

    What the registry does before it calls a factory is most of its job, so the
    tests need to see *whether* one was called and not only what came back.
    """

    def __init__(self) -> None:
        self.built: dict[ChannelKind, FakeChannel] = {}

    def table(self, *kinds: ChannelKind) -> dict[ChannelKind, ChannelFactory]:
        return {kind: self._factory(kind) for kind in kinds}

    def _factory(self, kind: ChannelKind) -> ChannelFactory:
        def factory(config: ChannelConfig) -> Channel:
            adapter = FakeChannel()
            self.built[kind] = adapter
            return adapter

        return factory


def test_the_registry_builds_only_the_enabled_channels() -> None:
    factories = RecordingFactories()
    registry = ChannelRegistry.from_config(
        telegram_config(), factories=factories.table("telegram", "whatsapp")
    )
    assert registry.enabled == ("telegram",)
    assert "whatsapp" not in registry
    assert set(factories.built) == {"telegram"}


def test_the_registry_builds_both_when_both_are_listed() -> None:
    factories = RecordingFactories()
    config = StubConfig(
        channels=("telegram", "whatsapp"),
        telegram_bot_token=StubSecret("token"),
        telegram_mode="polling",
        waba_phone_number_id=StubSecret("id"),
        waba_token=StubSecret("token"),
        waba_app_secret=StubSecret("secret"),
        waba_verify_token=StubSecret("verify"),
    )
    registry = ChannelRegistry.from_config(
        config, factories=factories.table("telegram", "whatsapp")
    )
    assert registry.enabled == ("telegram", "whatsapp")
    assert len(registry) == 2


def test_an_empty_channel_list_builds_nothing() -> None:
    registry = ChannelRegistry.from_config(StubConfig(), factories={})
    assert registry.enabled == ()


def test_a_disabled_channel_is_not_addressable() -> None:
    factories = RecordingFactories()
    registry = ChannelRegistry.from_config(telegram_config(), factories=factories.table("telegram"))
    assert registry.get("telegram") is factories.built["telegram"]
    with pytest.raises(ChannelNotEnabledError, match="whatsapp"):
        registry.get("whatsapp")


def test_the_registry_hands_out_the_protocol() -> None:
    registry = ChannelRegistry.from_config(
        telegram_config(), factories=RecordingFactories().table("telegram")
    )
    adapter: Channel = registry.get("telegram")
    assert isinstance(adapter, Channel)


@pytest.mark.parametrize(
    ("config", "missing"),
    [
        pytest.param(telegram_config(telegram_bot_token=None), "TELEGRAM_BOT_TOKEN", id="no token"),
        pytest.param(
            telegram_config(telegram_webhook_secret=None),
            "TELEGRAM_WEBHOOK_SECRET",
            id="webhook without secret",
        ),
        pytest.param(whatsapp_config(waba_token=None), "WABA_TOKEN", id="no waba token"),
        pytest.param(
            whatsapp_config(waba_app_secret=None), "WABA_APP_SECRET", id="no waba app secret"
        ),
    ],
)
def test_the_registry_refuses_a_channel_without_its_credentials(
    config: StubConfig, missing: str
) -> None:
    factories = RecordingFactories()
    with pytest.raises(MissingCredentialError, match=missing):
        ChannelRegistry.from_config(config, factories=factories.table("telegram", "whatsapp"))
    assert factories.built == {}, "the adapter was built before the credential check"


@pytest.mark.parametrize("blank", ["", "   ", "\n"], ids=["empty", "spaces", "newline"])
@pytest.mark.parametrize(
    ("build", "credential", "missing"),
    [
        pytest.param(telegram_config, "telegram_bot_token", "TELEGRAM_BOT_TOKEN", id="bot token"),
        pytest.param(
            telegram_config,
            "telegram_webhook_secret",
            "TELEGRAM_WEBHOOK_SECRET",
            id="webhook secret",
        ),
        pytest.param(
            whatsapp_config, "waba_phone_number_id", "WABA_PHONE_NUMBER_ID", id="phone number id"
        ),
        pytest.param(whatsapp_config, "waba_token", "WABA_TOKEN", id="waba token"),
        pytest.param(whatsapp_config, "waba_app_secret", "WABA_APP_SECRET", id="app secret"),
        pytest.param(whatsapp_config, "waba_verify_token", "WABA_VERIFY_TOKEN", id="verify token"),
    ],
)
def test_a_blank_credential_is_a_missing_credential(
    build: Callable[..., StubConfig], credential: str, missing: str, blank: str
) -> None:
    """An empty secret is not a credential, and presence is not the test.

    A `SecretStr("")` passes every `is None` check and reaches the factory, so
    the deployment starts and the failure moves to the first webhook — an
    incident instead of a deployment that never happened.
    """
    factories = RecordingFactories()
    with pytest.raises(MissingCredentialError, match=missing):
        ChannelRegistry.from_config(
            build(**{credential: StubSecret(blank)}),
            factories=factories.table("telegram", "whatsapp"),
        )
    assert factories.built == {}, "the adapter was built with an unusable credential"


def test_polling_needs_no_webhook_secret() -> None:
    """The secret authenticates the webhook; polling has none to authenticate."""
    registry = ChannelRegistry.from_config(
        telegram_config(telegram_mode="polling", telegram_webhook_secret=None),
        factories=RecordingFactories().table("telegram"),
    )
    assert registry.enabled == ("telegram",)


def test_the_shipped_table_covers_every_known_channel() -> None:
    assert set(ADAPTERS) == set(KNOWN_CHANNELS)


def test_an_adapter_that_does_not_exist_yet_says_so() -> None:
    """Until S02-T02 lands, enabling telegram fails by name and not by ImportError."""
    with pytest.raises(ChannelUnavailableError, match="telegram"):
        ChannelRegistry.from_config(telegram_config())


if TYPE_CHECKING:  # pragma: no cover

    def _settings_is_a_channel_config(settings: Settings) -> ChannelConfig:
        """mypy is the assertion: the real `Settings` satisfies the protocol."""
        return settings

    def _secret_str_is_a_credential(secret: SecretStr) -> None:
        _: ChannelConfig = StubConfig(telegram_bot_token=secret)


# --------------------------------------------------------------------------- #
# The concrete adapters stay inside channels/
# --------------------------------------------------------------------------- #

CONCRETE = ("fittrack.channels.telegram", "fittrack.channels.whatsapp")


def imported_modules(source: str, *, module_level_only: bool = False) -> set[str]:
    """Every absolute module path a source file imports, in any spelling."""
    tree = ast.parse(source)
    nodes = ast.iter_child_nodes(tree) if module_level_only else ast.walk(tree)
    names: set[str] = set()
    for node in nodes:
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            names.add(node.module)
            names.update(f"{node.module}.{alias.name}" for alias in node.names)
    return names


def names_a_concrete_channel(module: str) -> bool:
    segments = module.split(".")
    return any(segments[: len(target.split("."))] == target.split(".") for target in CONCRETE)


def test_the_import_check_sees_what_it_should() -> None:
    assert names_a_concrete_channel("fittrack.channels.telegram.adapter")
    assert names_a_concrete_channel("fittrack.channels.whatsapp")
    assert not names_a_concrete_channel("fittrack.channels.base")
    assert not names_a_concrete_channel("fittrack.channels.registry")


def test_nothing_outside_channels_imports_a_concrete_adapter() -> None:
    """The registry hands out a `Channel`; the rest of `src/` knows nothing else.

    `scripts/` is out of scope on purpose: `bootstrap.py` calls `setWebhook`,
    which is Telegram operations and not application code (spec 18.2).
    """
    offenders = [
        path.relative_to(ROOT).as_posix()
        for path in sorted(SRC.rglob("*.py"))
        if "fittrack/channels/" not in path.as_posix()
        for module in imported_modules(path.read_text(encoding="utf-8"))
        if names_a_concrete_channel(module)
    ]
    assert not offenders, f"{offenders} import a concrete channel; the registry returns Channel"


def test_the_registry_does_not_import_an_adapter_at_module_level() -> None:
    """A lazy import is what lets the registry name every channel and load one."""
    source = (SRC / "fittrack" / "channels" / "registry.py").read_text(encoding="utf-8")
    assert not {
        module
        for module in imported_modules(source, module_level_only=True)
        if names_a_concrete_channel(module)
    }
