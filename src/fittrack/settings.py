"""Application configuration.

Every credential is required and has no default. A silent fallback for a secret
is worse than a crash: the process starts, talks to the wrong place, and nobody
notices until data is somewhere it should not be.

Behaviour knobs (timeouts, thresholds) do have defaults, taken from Apêndice A
of doc/spec.md, because a wrong timeout is visible and a wrong credential is not.
"""

from __future__ import annotations

import base64
import binascii
from functools import lru_cache
from typing import Annotated

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# AES-256-GCM. Not negotiable: a shorter key silently weakens every encrypted
# column in §22.2.
ENCRYPTION_KEY_BYTES = 32


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
    waba_token: SecretStr
    waba_app_secret: SecretStr
    waba_verify_token: SecretStr

    # --- LLM providers (§7.2) ---
    xai_api_key: SecretStr
    anthropic_api_key: SecretStr
    openai_api_key: SecretStr  # embeddings only
    groq_api_key: SecretStr  # STT only

    # --- Infrastructure (§3.1) ---
    database_url: str
    redis_url: str
    qdrant_url: str
    qdrant_api_key: SecretStr | None = None

    # --- Encryption (§22.2) ---
    fittrack_encryption_key: SecretStr

    # --- Observability (§20). Optional: absent means "do not export". ---
    langfuse_host: str | None = None
    langfuse_public_key: SecretStr | None = None
    langfuse_secret_key: SecretStr | None = None
    otel_exporter_otlp_endpoint: str | None = None

    # --- Billing (§19.4) ---
    mercadopago_access_token: SecretStr | None = None
    mercadopago_webhook_secret: SecretStr | None = None

    # --- Behaviour, from Apêndice A ---
    session_idle_timeout_min: Annotated[int, Field(gt=0)] = 90
    session_max_duration_min: Annotated[int, Field(gt=0)] = 240
    debounce_window_s: Annotated[int, Field(gt=0)] = 10
    interrupt_ttl_min: Annotated[int, Field(gt=0)] = 20
    ack_confidence_threshold: Annotated[float, Field(ge=0.0, le=1.0)] = 0.85

    @field_validator("database_url")
    @classmethod
    def _require_async_driver(cls, value: str) -> str:
        """A sync driver blocks the event loop under load, and the symptom
        (creeping latency) points nowhere near the cause."""
        if "+asyncpg" not in value:
            raise ValueError(
                "database_url must use the asyncpg driver "
                "(postgresql+asyncpg://...), not a synchronous one"
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
