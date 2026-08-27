"""Typed configuration and the secret boundary (spec sections 7.2, 19.3, 22, Apêndice A).

Two properties this module exists to hold:

1. **An invalid environment fails at boot**, before anything opens a connection.
   A missing credential found on the first webhook is an incident; the same one
   found at startup is a deployment that never happened.
2. **A secret never reaches a repr, a serialisation or a log.** Everything
   sensitive is a `SecretStr`, including the connection URLs — a DSN carries the
   Postgres password, so the whole string is a secret, not just a field of it.

Model names are deliberately absent. They live in `config/models.yaml` and are
resolved by role (CLAUDE.md, invariant 4).
"""

from __future__ import annotations

import base64
import binascii
import json
import re
from functools import lru_cache
from typing import Annotated, Literal, Self
from urllib.parse import parse_qs, urlsplit

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ChannelKind = Literal["telegram", "whatsapp"]
TelegramMode = Literal["webhook", "polling"]

# `key_version` is a positive SMALLINT in the schema and two big-endian bytes in
# the blob header (spec 22.2). Both ends agree on this range; nothing else fits.
MIN_KEY_VERSION = 1
MAX_KEY_VERSION = 32767
KEY_BYTES = 32

# Telegram accepts 1..256 characters of this alphabet in the secret token and
# rejects the request outright otherwise (spec 18.2).
WEBHOOK_SECRET_ALPHABET = re.compile(r"^[A-Za-z0-9_-]{1,256}$")
# The floor is ours, not Telegram's. Their limit would accept one character;
# the spec asks for 32 random bytes, because this value authenticates every
# update the webhook receives (spec 18.2).
MIN_WEBHOOK_SECRET_CHARS = 43  # 32 bytes, url-safe base64


class Settings(BaseSettings):
    """The whole environment contract, validated once per process."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,  # one process, one configuration
        case_sensitive=False,
    )

    # --- infrastructure ----------------------------------------------------
    database_url: SecretStr
    redis_url: SecretStr
    qdrant_url: str
    qdrant_api_key: SecretStr | None = None
    fittrack_tls_ca_file: str | None = None

    # --- channels ----------------------------------------------------------
    # The registry only builds an adapter for what is listed here, and this
    # class refuses to boot if a listed channel has no credentials.
    fittrack_channels: str = ""
    telegram_bot_token: SecretStr | None = None
    telegram_webhook_secret: SecretStr | None = None
    telegram_mode: TelegramMode = "polling"
    telegram_webhook_url: str | None = None
    waba_phone_number_id: SecretStr | None = None
    waba_token: SecretStr | None = None
    waba_app_secret: SecretStr | None = None
    waba_verify_token: SecretStr | None = None

    # --- providers (ADR-0001) ---------------------------------------------
    groq_api_key: SecretStr | None = None
    anthropic_api_key: SecretStr | None = None
    openai_api_key: SecretStr | None = None
    xai_api_key: SecretStr | None = None

    # --- observability -----------------------------------------------------
    langfuse_host: str | None = None
    langfuse_public_key: SecretStr | None = None
    langfuse_secret_key: SecretStr | None = None
    otel_exporter_otlp_endpoint: str | None = None

    # --- billing -----------------------------------------------------------
    mercadopago_access_token: SecretStr | None = None
    mercadopago_webhook_secret: SecretStr | None = None

    # --- column encryption (section 22.2) ----------------------------------
    fittrack_encryption_keys: SecretStr
    fittrack_active_key_version: int
    fittrack_identity_pepper: SecretStr

    # --- configuration files ----------------------------------------------
    fittrack_config_dir: str = "config"

    # --- behaviour (Apêndice A) -------------------------------------------
    session_idle_timeout_min: Annotated[int, Field(gt=0)] = 90
    session_max_duration_min: Annotated[int, Field(gt=0)] = 240
    debounce_window_s: Annotated[int, Field(gt=0)] = 10
    interrupt_ttl_min: Annotated[int, Field(gt=0)] = 20
    ack_confidence_threshold: Annotated[float, Field(ge=0.0, le=1.0)] = 0.85
    channel_link_ttl_min: Annotated[int, Field(gt=0)] = 10
    graph_recursion_limit: Annotated[int, Field(gt=0)] = 40
    checkpoint_retention_days: Annotated[int, Field(gt=0)] = 30

    # ------------------------------------------------------------- normalising

    # Optional credentials only: a blank `FITTRACK_CHANNELS` means "no channel",
    # which is a value, while a blank token means the token was never set.
    @field_validator(
        "telegram_bot_token",
        "telegram_webhook_secret",
        "telegram_webhook_url",
        "waba_phone_number_id",
        "waba_token",
        "waba_app_secret",
        "waba_verify_token",
        "groq_api_key",
        "anthropic_api_key",
        "openai_api_key",
        "xai_api_key",
        "qdrant_api_key",
        "langfuse_public_key",
        "langfuse_secret_key",
        "mercadopago_access_token",
        "mercadopago_webhook_secret",
        mode="before",
    )
    @classmethod
    def _blank_is_absent(cls, value: object) -> object:
        """An empty variable is an unset one.

        `TELEGRAM_BOT_TOKEN=` in a `.env` file is how a credential is *left
        blank*, not how it is set to the empty string. Reading it as present is
        how a channel gets enabled with no way to talk to it — and the failure
        then surfaces on the first webhook instead of at boot, which is the
        whole thing this class exists to prevent.
        """
        if isinstance(value, str) and not value.strip():
            return None
        return value

    # ----------------------------------------------------------------- URLs

    # Validation that touches a SecretStr lives in `mode="after"` validators
    # below, never in a field validator. Pydantic quotes the rejected input in
    # the error it raises, so a field validator on a secret publishes it to
    # every log that catches the exception.

    @field_validator("qdrant_url")
    @classmethod
    def _qdrant_must_be_tls(cls, value: str) -> str:
        if not value.startswith("https://"):
            raise ValueError("QDRANT_URL must use https (spec 22.1)")
        return value

    # ------------------------------------------------------- column encryption

    @property
    def encryption_keys(self) -> dict[int, bytes]:
        """The versioned keyring. Decoded on access, never stored decoded."""
        return _parse_keyring(self.fittrack_encryption_keys.get_secret_value())

    @property
    def active_key_version(self) -> int:
        return self.fittrack_active_key_version

    @model_validator(mode="after")
    def _connection_urls_are_encrypted(self) -> Self:
        """Spec 22.1, checked without quoting the DSN — it carries the password."""
        # Parsed, not matched as a substring: `sslmode=verify-full` can appear in
        # a password, a path or another parameter while the effective one says
        # `disable`. verify-ca would not do either — it accepts any certificate
        # this CA signed, which in the compose topology is every service.
        if _sslmode(self.database_url) != "verify-full":
            raise ValueError(
                "DATABASE_URL must set sslmode=verify-full as its effective query "
                "parameter (spec 22.1)"
            )
        if not self.redis_url.get_secret_value().startswith("rediss://"):
            raise ValueError("REDIS_URL must use the rediss:// scheme (spec 22.1)")
        return self

    @model_validator(mode="after")
    def _keyring_is_usable(self) -> Self:
        keyring = self.encryption_keys
        if not keyring:
            raise ValueError("FITTRACK_ENCRYPTION_KEYS must define at least one key version")
        if self.fittrack_active_key_version not in keyring:
            raise ValueError(
                f"FITTRACK_ACTIVE_KEY_VERSION {self.fittrack_active_key_version} "
                "is not in the keyring"
            )
        return self

    @model_validator(mode="after")
    def _pepper_is_present_and_independent(self) -> Self:
        """The two rotate by separate procedures (spec 22.2); shared material couples them."""
        pepper = self.fittrack_identity_pepper.get_secret_value()
        if not pepper.strip():
            raise ValueError("FITTRACK_IDENTITY_PEPPER must not be empty")
        # Compared as bytes, not as text. The pepper is used as raw bytes and a
        # key is base64; a pepper of 32 literal "A" characters and a keyring
        # holding the base64 of 32 "A" bytes are different strings feeding both
        # cryptographic operations the same material.
        pepper_bytes = pepper.encode()
        if pepper in self.fittrack_encryption_keys.get_secret_value() or any(
            key == pepper_bytes for key in self.encryption_keys.values()
        ):
            raise ValueError(
                "FITTRACK_IDENTITY_PEPPER must be independent of FITTRACK_ENCRYPTION_KEYS"
            )
        return self

    # ---------------------------------------------------------------- channels

    @property
    def channels(self) -> tuple[ChannelKind, ...]:
        return _parse_channels(self.fittrack_channels)

    @field_validator("fittrack_channels")
    @classmethod
    def _channels_are_known(cls, value: str) -> str:
        _parse_channels(value)
        return value

    @model_validator(mode="after")
    def _enabled_channels_have_credentials(self) -> Self:
        """Listing a channel is a promise the adapter can be built (Apêndice A)."""
        if "telegram" in self.channels:
            if self.telegram_bot_token is None:
                raise ValueError("FITTRACK_CHANNELS lists telegram but TELEGRAM_BOT_TOKEN is unset")
            if self.telegram_mode == "webhook":
                if self.telegram_webhook_secret is None:
                    raise ValueError("TELEGRAM_MODE=webhook requires TELEGRAM_WEBHOOK_SECRET")
                secret = self.telegram_webhook_secret.get_secret_value()
                if not WEBHOOK_SECRET_ALPHABET.match(secret):
                    raise ValueError(
                        "TELEGRAM_WEBHOOK_SECRET must be 1-256 characters of the alphabet "
                        "Telegram accepts (A-Z a-z 0-9 _ -)"
                    )
                if len(secret) < MIN_WEBHOOK_SECRET_CHARS:
                    raise ValueError(
                        f"TELEGRAM_WEBHOOK_SECRET must be at least "
                        f"{MIN_WEBHOOK_SECRET_CHARS} characters — 32 random bytes, "
                        "since it authenticates every update (spec 18.2)"
                    )
                if not self.telegram_webhook_url:
                    raise ValueError("TELEGRAM_MODE=webhook requires TELEGRAM_WEBHOOK_URL")
                if not self.telegram_webhook_url.startswith("https://"):
                    # Telegram refuses to register a non-HTTPS webhook (spec
                    # 3.1), so without this the failure lands at setWebhook.
                    raise ValueError("TELEGRAM_WEBHOOK_URL must use https")

        if "whatsapp" in self.channels:
            missing = [
                name
                for name, value in (
                    ("WABA_PHONE_NUMBER_ID", self.waba_phone_number_id),
                    ("WABA_TOKEN", self.waba_token),
                    ("WABA_APP_SECRET", self.waba_app_secret),
                    ("WABA_VERIFY_TOKEN", self.waba_verify_token),
                )
                if value is None
            ]
            if missing:
                raise ValueError(f"FITTRACK_CHANNELS lists whatsapp but {', '.join(missing)} unset")
        return self


KNOWN_CHANNELS: tuple[ChannelKind, ...] = ("telegram", "whatsapp")


def _parse_channels(raw: str) -> tuple[ChannelKind, ...]:
    names = [name.strip() for name in raw.split(",") if name.strip()]
    unknown = [name for name in names if name not in KNOWN_CHANNELS]
    if unknown:
        raise ValueError(f"unknown channel(s) in FITTRACK_CHANNELS: {', '.join(unknown)}")
    return tuple(channel for channel in KNOWN_CHANNELS if channel in names)


def _parse_keyring(raw: str) -> dict[int, bytes]:
    """Decode `{"version": "<base64 of 32 bytes>"}`.

    Every error message here names the *version* and never the material: a
    validation error that quotes the value it rejected is the most common way a
    key reaches a log.
    """
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as error:
        raise ValueError("FITTRACK_ENCRYPTION_KEYS must be a JSON object") from error
    if not isinstance(parsed, dict):
        raise ValueError("FITTRACK_ENCRYPTION_KEYS must be a JSON object")

    keyring: dict[int, bytes] = {}
    for raw_version, raw_key in parsed.items():
        try:
            version = int(raw_version)
        except (TypeError, ValueError) as error:
            raise ValueError("key version must be an integer") from error
        if version in keyring:
            # "1" and "01" both normalise to 1, and the later would silently
            # win — selecting different master material for a version already in
            # the database, which makes every ciphertext under the loser
            # undecryptable.
            raise ValueError(f"duplicate key version {version} after normalisation")
        if not MIN_KEY_VERSION <= version <= MAX_KEY_VERSION:
            raise ValueError(
                f"key version {version} is outside {MIN_KEY_VERSION}..{MAX_KEY_VERSION}"
            )
        if not isinstance(raw_key, str):
            raise ValueError(f"key {version} must be a base64 string")
        try:
            key = base64.b64decode(raw_key, validate=True)
        except (binascii.Error, ValueError) as error:
            raise ValueError(f"key {version} is not valid base64") from error
        if len(key) != KEY_BYTES:
            raise ValueError(f"key {version} must decode to {KEY_BYTES} bytes")
        keyring[version] = key
    return keyring


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """The process-wide configuration, built once and never rebuilt."""
    return Settings()


def _sslmode(dsn: SecretStr) -> str | None:
    """The effective `sslmode` of a DSN, or None when it sets none."""
    values = parse_qs(urlsplit(dsn.get_secret_value()).query).get("sslmode")
    return values[-1] if values else None
