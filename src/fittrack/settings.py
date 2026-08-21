"""Application configuration.

Every credential is required, must be non-empty, and has no default. A silent
fallback for a secret is worse than a crash: the process starts, talks to the
wrong place, and nobody notices until data is somewhere it should not be.

Behaviour knobs (timeouts, thresholds) do have defaults, taken from Apêndice A
of doc/spec.md, because a wrong timeout is visible and a wrong credential is not.
"""

from __future__ import annotations

import base64
import binascii
from functools import lru_cache
from typing import Annotated
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# AES-256-GCM. Not negotiable: a shorter key silently weakens every encrypted
# column in §22.2.
ENCRYPTION_KEY_BYTES = 32

# Every connection string can carry a password, so none of them is a plain str.
Secret = SecretStr


class Settings(BaseSettings):
    """Resolved once at startup. Never re-read per request."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- WhatsApp Cloud API (§18) ---
    waba_phone_number_id: str
    waba_token: Secret
    waba_app_secret: Secret
    waba_verify_token: Secret

    # --- LLM providers (§7.2) ---
    xai_api_key: Secret
    anthropic_api_key: Secret
    openai_api_key: Secret  # embeddings only
    groq_api_key: Secret  # STT only

    # --- Infrastructure (§3.1). Secret because a URI can embed a password. ---
    database_url: Secret
    redis_url: Secret
    qdrant_url: Secret
    qdrant_api_key: Secret | None = None

    # --- Encryption (§22.2) ---
    fittrack_encryption_key: Secret

    # --- Observability (§20). Optional: absent means "do not export". ---
    langfuse_host: str | None = None
    langfuse_public_key: Secret | None = None
    langfuse_secret_key: Secret | None = None
    otel_exporter_otlp_endpoint: str | None = None

    # --- Billing (§19.4) ---
    mercadopago_access_token: Secret | None = None
    mercadopago_webhook_secret: Secret | None = None

    # --- Behaviour, from Apêndice A ---
    session_idle_timeout_min: Annotated[int, Field(gt=0)] = 90
    session_max_duration_min: Annotated[int, Field(gt=0)] = 240
    debounce_window_s: Annotated[int, Field(gt=0)] = 10
    interrupt_ttl_min: Annotated[int, Field(gt=0)] = 20
    ack_confidence_threshold: Annotated[float, Field(ge=0.0, le=1.0)] = 0.85

    @field_validator(
        "waba_phone_number_id",
        "waba_token",
        "waba_app_secret",
        "waba_verify_token",
        "xai_api_key",
        "anthropic_api_key",
        "openai_api_key",
        "groq_api_key",
        "database_url",
        "redis_url",
        "qdrant_url",
        "fittrack_encryption_key",
    )
    @classmethod
    def _reject_blank(cls, value: str | SecretStr) -> str | SecretStr:
        """An empty string satisfies a required field but not a credential.

        .env.example ships every variable with an empty value, so without this
        the app starts with a blank WABA token and only fails at the first API
        call -- exactly the deferred failure the required-field design exists
        to prevent.
        """
        raw = value.get_secret_value() if isinstance(value, SecretStr) else value
        if not raw.strip():
            raise ValueError("must not be empty")
        return value

    @field_validator("database_url")
    @classmethod
    def _require_async_driver(cls, value: SecretStr) -> SecretStr:
        """A synchronous driver blocks the event loop under load, and the
        symptom (creeping latency) points nowhere near the cause.

        Parses the scheme rather than searching the string: a substring check
        accepts `postgresql://u:p@h/db?note=+asyncpg` and the typo
        `postgresql+asyncpgx://`, both of which select a sync dialect.
        """
        scheme = urlsplit(value.get_secret_value()).scheme
        dialect, _, driver = scheme.partition("+")

        if dialect != "postgresql" or driver != "asyncpg":
            raise ValueError(
                f"database_url must use the postgresql+asyncpg driver, got scheme {scheme!r}"
            )
        return value

    @field_validator("fittrack_encryption_key")
    @classmethod
    def _require_32_byte_key(cls, value: SecretStr) -> SecretStr:
        """Fail at startup, not at the first write to an encrypted column."""
        try:
            raw = base64.b64decode(value.get_secret_value(), validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError(
                "fittrack_encryption_key must be valid base64 of 32 raw bytes"
            ) from exc

        if len(raw) != ENCRYPTION_KEY_BYTES:
            raise ValueError(
                f"fittrack_encryption_key must decode to exactly "
                f"{ENCRYPTION_KEY_BYTES} bytes for AES-256-GCM, got {len(raw)}"
            )
        return value

    @property
    def encryption_key(self) -> bytes:
        """The raw key. Call sites should hold this as briefly as possible."""
        return base64.b64decode(self.fittrack_encryption_key.get_secret_value())


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached accessor. Import this, not the class, so tests can clear the cache."""
    return Settings()
