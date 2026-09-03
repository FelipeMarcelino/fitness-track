"""Which adapters this process has, built from `FITTRACK_CHANNELS` (spec 18.1).

The registry is the seam that keeps `Channel` an abstraction. Everything
downstream — the ingress, the outbound service, `deliver` — asks it for a
channel by kind and receives a `Channel`; none of them ever names a concrete
adapter, and `tests/unit/test_channel_contract.py` checks that across `src/`.

Two rules, both from Apêndice A:

1. **Only what is listed is built.** A deployment that has WhatsApp credentials
   but does not list the channel has no WhatsApp adapter — the environment says
   what runs, not the presence of a secret.
2. **A listed channel without usable credentials fails here.** Absent, blank,
   wearing whitespace, or — for the webhook secret, which authenticates every
   update — the wrong shape. An environment variable that is set and empty is a
   credential in name only; so is a token that kept the newline of the file it
   was mounted from, and so is a three-character shared secret.
   `Settings` already refuses to boot in that state; this check exists because
   the registry is also fed by scripts and tests that build a configuration
   directly, and because a half-built adapter fails on the first webhook instead
   of at startup — which is an incident rather than a deployment that never
   happened.

3. **An adapter answers to the kind it is filed under.** The types cannot see
   the difference — `kind` is the union of both channel names — so the registry
   checks it once, where the adapters go in.

The adapters are imported lazily inside their factories. That is what lets this
module name every channel while loading only the one in use, and it keeps the
protocol code out of the import graph of anything that merely wants the type.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from typing import TYPE_CHECKING, Any, Protocol

from fittrack.channels.base import Channel
from fittrack.settings import (
    KNOWN_CHANNELS,
    MIN_WEBHOOK_SECRET_CHARS,
    WEBHOOK_SECRET_ALPHABET,
    ChannelKind,
    TelegramMode,
)

if TYPE_CHECKING:
    from pydantic import SecretStr

__all__ = [
    "ADAPTERS",
    "ChannelConfig",
    "ChannelFactory",
    "ChannelKindMismatchError",
    "ChannelNotEnabledError",
    "ChannelRegistry",
    "ChannelRegistryError",
    "ChannelUnavailableError",
    "MissingCredentialError",
    "bot_fingerprint",
    "build_telegram_poller",
]


class ChannelRegistryError(Exception):
    """The registry cannot serve a channel that was asked of it."""


class MissingCredentialError(ChannelRegistryError):
    """A listed channel has no credentials to build an adapter with."""


class ChannelNotEnabledError(ChannelRegistryError, LookupError):
    """A channel was addressed that this process does not run."""


class ChannelKindMismatchError(ChannelRegistryError):
    """An adapter was filed under a kind it does not answer to."""


class ChannelUnavailableError(ChannelRegistryError, NotImplementedError):
    """A channel is enabled but its adapter is not implemented yet."""


class ChannelConfig(Protocol):
    """The slice of `Settings` the registry reads.

    A protocol rather than the class itself, so the registry's rules can be
    exercised against configurations `Settings` refuses to represent — a listed
    channel with a missing credential being precisely the one that matters here.
    Every member is read-only; `Settings` satisfies it structurally.
    """

    @property
    def channels(self) -> tuple[ChannelKind, ...]: ...

    @property
    def telegram_bot_token(self) -> SecretStr | None: ...

    @property
    def telegram_mode(self) -> TelegramMode: ...

    @property
    def telegram_webhook_secret(self) -> SecretStr | None: ...

    @property
    def waba_phone_number_id(self) -> SecretStr | None: ...

    @property
    def waba_token(self) -> SecretStr | None: ...

    @property
    def waba_app_secret(self) -> SecretStr | None: ...

    @property
    def waba_verify_token(self) -> SecretStr | None: ...


ChannelFactory = Callable[[ChannelConfig], Channel]


def _unusable(name: str, secret: SecretStr | None) -> str | None:
    """What to say about a credential that cannot be used, if it cannot.

    Presence is the wrong question, twice over. A `SecretStr("")` — an
    environment variable that is set and empty is the ordinary way to get one —
    satisfies every `is None` check; so does a token that kept the newline of
    the secret file it was mounted from. Both reach the factory, the process
    starts, and the failure moves to the first call the adapter makes to the
    provider. That is an incident; the point of this check is a deployment that
    never happened.

    Padding is refused rather than trimmed. The registry does not own the
    configuration, and quietly repairing a broken one hides the deployment that
    needs fixing. The complaint never quotes the value.
    """
    if secret is None:
        return name
    value = secret.get_secret_value()
    if not value.strip():
        return name
    if value != value.strip():
        return f"{name} without the whitespace around it"
    return None


def _unusable_webhook_secret(secret: SecretStr) -> str | None:
    """Why the webhook secret cannot authenticate an update, if it cannot.

    The rules and the constants are `Settings`', imported rather than restated:
    two spellings of "43 characters, url-safe alphabet" would drift, and this
    one would be the copy nobody remembers to update. The reason never quotes
    the value — a rejection that publishes the secret to every log that catches
    it is a worse outcome than the secret it rejected.
    """
    value = secret.get_secret_value()
    if not WEBHOOK_SECRET_ALPHABET.match(value):
        return "must be 1-256 characters of A-Z a-z 0-9 _ - , which is what Telegram accepts"
    if len(value) < MIN_WEBHOOK_SECRET_CHARS:
        return f"must be at least {MIN_WEBHOOK_SECRET_CHARS} characters, being 32 random bytes"
    return None


def _missing_telegram_credentials(config: ChannelConfig) -> list[str]:
    missing = []
    if complaint := _unusable("TELEGRAM_BOT_TOKEN", config.telegram_bot_token):
        missing.append(complaint)
    # Only the webhook needs a secret: it is what authenticates every update
    # (spec 18.2). Polling authenticates itself by holding the bot token.
    if config.telegram_mode == "webhook":
        secret = config.telegram_webhook_secret
        if complaint := _unusable("TELEGRAM_WEBHOOK_SECRET", secret):
            missing.append(complaint)
        elif secret is not None and (why := _unusable_webhook_secret(secret)):
            # Present but unusable is the same failure as absent, one step
            # later: `setWebhook` refuses the first, and the second is a shared
            # secret short enough to guess. Both are startup problems.
            missing.append(f"TELEGRAM_WEBHOOK_SECRET that {why}")
    return missing


def _missing_whatsapp_credentials(config: ChannelConfig) -> list[str]:
    return [
        complaint
        for name, value in (
            ("WABA_PHONE_NUMBER_ID", config.waba_phone_number_id),
            ("WABA_TOKEN", config.waba_token),
            ("WABA_APP_SECRET", config.waba_app_secret),
            ("WABA_VERIFY_TOKEN", config.waba_verify_token),
        )
        if (complaint := _unusable(name, value))
    ]


CREDENTIALS: Mapping[ChannelKind, Callable[[ChannelConfig], list[str]]] = {
    "telegram": _missing_telegram_credentials,
    "whatsapp": _missing_whatsapp_credentials,
}


def _build_telegram(config: ChannelConfig) -> Channel:
    # Imported here, not at module level: this is what lets the registry name
    # every channel while loading only the one in use, and it keeps `httpx` out
    # of the import graph of anything that merely wants the `Channel` type.
    import httpx

    from fittrack.channels.telegram.adapter import TelegramAdapter
    from fittrack.channels.telegram.client import DEFAULT_TIMEOUT_SECONDS, TelegramClient

    token = config.telegram_bot_token
    if token is None:  # pragma: no cover - the credential check ran first
        raise MissingCredentialError("telegram needs TELEGRAM_BOT_TOKEN")

    # The pool is built here and owned by the process, not opened per send. The
    # adapter never constructs a connection of its own.
    http = httpx.AsyncClient(timeout=DEFAULT_TIMEOUT_SECONDS)
    return TelegramAdapter(
        TelegramClient(token, http=http),
        # Polling has no webhook to authenticate, and an adapter holding a
        # secret it must not use is worse than one that refuses (spec 18.2).
        webhook_secret=(
            config.telegram_webhook_secret if config.telegram_mode == "webhook" else None
        ),
    )


def bot_fingerprint(token: SecretStr) -> str:
    """Enough of `TELEGRAM_BOT_TOKEN` to tell two bots apart, never the token.

    Not the token itself — same reason `external_id` is never a Redis key
    elsewhere in this codebase. Every Redis key this process derives from the
    bot (the poller's offset, the webhook dedup reservation) is namespaced by
    this, so a dev Redis volume that outlives a `TELEGRAM_BOT_TOKEN` change
    cannot hand the new bot the old bot's state (spec 18.2 review).
    """
    import hashlib

    return hashlib.sha256(token.get_secret_value().encode()).hexdigest()[:16]


def build_telegram_poller(config: ChannelConfig, *, redis: Any) -> Any:
    """The dev-only `getUpdates` transport of S02-T08, wired the way `_build_telegram` is.

    Imported lazily for the same reason: the ingress wiring that calls this
    lives outside `channels/`, and this is the one door it may use to reach a
    Telegram type without naming it (`tests/unit/test_channel_contract.py`).

    `redis` is the raw client the offset store reads and writes; the caller
    owns its connection. Nothing here does I/O — `httpx.AsyncClient()` and
    `RedisOffsetStore` are both lazy, so this is as safe to unit test as
    `_build_telegram` is.
    """
    import httpx

    from fittrack.channels.telegram.client import DEFAULT_TIMEOUT_SECONDS, TelegramClient
    from fittrack.channels.telegram.polling import RedisOffsetStore, TelegramPoller

    token = config.telegram_bot_token
    if token is None:
        raise MissingCredentialError("telegram polling needs TELEGRAM_BOT_TOKEN")

    fingerprint = bot_fingerprint(token)

    http = httpx.AsyncClient(timeout=DEFAULT_TIMEOUT_SECONDS)
    return TelegramPoller(
        client=TelegramClient(token, http=http),
        offsets=RedisOffsetStore(redis, bot_fingerprint=fingerprint),
    )


def _build_whatsapp(config: ChannelConfig) -> Channel:
    # Phase 2.0 (spec 18.3). The enum, the error classes and this entry are the
    # only parts of WhatsApp that exist before then, on purpose.
    raise ChannelUnavailableError("the whatsapp adapter arrives with phase 2.0 (spec 18.3)")


ADAPTERS: Mapping[ChannelKind, ChannelFactory] = {
    "telegram": _build_telegram,
    "whatsapp": _build_whatsapp,
}


class ChannelRegistry:
    """The adapters this process runs, keyed by kind."""

    def __init__(self, adapters: Mapping[ChannelKind, Channel]) -> None:
        # A factory under the wrong key, or one that returns the other channel's
        # adapter, type-checks perfectly: `kind` is the union of both names. It
        # would be handed out by `get`, reported as enabled, and fail at
        # `ensure_addressable` on every send — a per-message failure where a
        # per-deployment one was available. The constructor is where both doors
        # meet, so the check lives here rather than in `from_config`.
        for kind, adapter in adapters.items():
            if adapter.kind != kind:
                raise ChannelKindMismatchError(
                    f"the adapter filed under {kind} answers to {adapter.kind}"
                )
        self._adapters: dict[ChannelKind, Channel] = dict(adapters)

    @classmethod
    def from_config(
        cls,
        config: ChannelConfig,
        *,
        factories: Mapping[ChannelKind, ChannelFactory] | None = None,
    ) -> ChannelRegistry:
        """Build an adapter for each listed channel, credentials first.

        The credential check runs over every listed channel before the first
        factory is called: a deployment missing two secrets should be told both,
        and none of it should half-start.
        """
        table = ADAPTERS if factories is None else factories
        missing = {kind: names for kind in config.channels if (names := CREDENTIALS[kind](config))}
        if missing:
            detail = "; ".join(
                f"{kind} needs {', '.join(names)}" for kind, names in missing.items()
            )
            raise MissingCredentialError(
                f"FITTRACK_CHANNELS lists channels it cannot build: {detail}"
            )

        adapters: dict[ChannelKind, Channel] = {}
        for kind in config.channels:
            factory = table.get(kind)
            if factory is None:
                raise ChannelUnavailableError(f"no adapter is registered for {kind}")
            adapters[kind] = factory(config)
        return cls(adapters)

    def get(self, kind: ChannelKind) -> Channel:
        """The adapter for a kind, or a refusal naming what this process runs."""
        try:
            return self._adapters[kind]
        except KeyError:
            enabled = ", ".join(self.enabled) or "none"
            raise ChannelNotEnabledError(
                f"{kind} is not in FITTRACK_CHANNELS (enabled: {enabled})"
            ) from None

    @property
    def enabled(self) -> tuple[ChannelKind, ...]:
        """Enabled kinds, in the order of `KNOWN_CHANNELS`."""
        return tuple(kind for kind in KNOWN_CHANNELS if kind in self._adapters)

    def __contains__(self, kind: object) -> bool:
        return kind in self._adapters

    def __iter__(self) -> Iterator[ChannelKind]:
        return iter(self.enabled)

    def __len__(self) -> int:
        return len(self._adapters)
