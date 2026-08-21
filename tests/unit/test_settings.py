"""Settings must fail loudly on a missing secret, never fall back to a default.

A silent default for a credential is worse than a crash: the app starts, talks to
the wrong place, and nobody notices until data is somewhere it should not be.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from fittrack.settings import Settings

REQUIRED = {
    "WABA_PHONE_NUMBER_ID": "1234567890",
    "WABA_TOKEN": "EAAG-test-token",
    "WABA_APP_SECRET": "app-secret",
    "WABA_VERIFY_TOKEN": "verify-token",
    "XAI_API_KEY": "xai-key",
    "ANTHROPIC_API_KEY": "anthropic-key",
    "OPENAI_API_KEY": "openai-key",
    "GROQ_API_KEY": "groq-key",
    "DATABASE_URL": "postgresql+asyncpg://u:sup3rs3cr3t@localhost:5432/db",
    "REDIS_URL": "redis://localhost:6379/0",
    "QDRANT_URL": "http://localhost:6333",
    "FITTRACK_ENCRYPTION_KEY": "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA=",  # 32 bytes exatos
}


def _build(**overrides: object) -> Settings:
    """Build Settings isolated from any local .env file.

    Without _env_file=None the tests read the developer's .env, so a test that
    deletes a variable still finds it there. That passes in CI (no .env) and
    fails on a developer machine, which is the worst kind of flake.
    """
    return Settings(_env_file=None, **overrides)  # type: ignore[arg-type]


def _env(monkeypatch: pytest.MonkeyPatch, **overrides: str | None) -> None:
    """Load the full required environment, then apply overrides (None deletes)."""
    for key, value in {**REQUIRED, **overrides}.items():
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)


def test_loads_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    _env(monkeypatch)
    settings = _build()

    assert settings.waba_phone_number_id == "1234567890"
    assert settings.database_url.get_secret_value().startswith("postgresql+asyncpg://")
    assert settings.redis_url.get_secret_value() == "redis://localhost:6379/0"


@pytest.mark.parametrize("missing", sorted(REQUIRED))
def test_missing_required_variable_raises(monkeypatch: pytest.MonkeyPatch, missing: str) -> None:
    """Every required variable is required. No exceptions, no quiet defaults."""
    _env(monkeypatch, **{missing: None})

    with pytest.raises(ValidationError) as exc:
        _build()

    assert missing.lower() in str(exc.value).lower()


def test_secrets_are_not_repr_visible(monkeypatch: pytest.MonkeyPatch) -> None:
    """A stack trace or a log line must never leak a credential."""
    _env(monkeypatch)
    settings = _build()

    rendered = repr(settings) + str(settings)
    for secret in (
        REQUIRED["WABA_TOKEN"],
        REQUIRED["WABA_APP_SECRET"],
        REQUIRED["XAI_API_KEY"],
        REQUIRED["ANTHROPIC_API_KEY"],
        REQUIRED["FITTRACK_ENCRYPTION_KEY"],
        # A connection URI embeds a password; it leaks like any other secret.
        REQUIRED["DATABASE_URL"],
        REQUIRED["REDIS_URL"],
    ):
        assert secret not in rendered


def test_secret_values_are_readable_when_asked(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hidden in repr, available to code that explicitly unwraps them."""
    _env(monkeypatch)
    settings = _build()

    assert settings.waba_token.get_secret_value() == REQUIRED["WABA_TOKEN"]
    assert settings.xai_api_key.get_secret_value() == REQUIRED["XAI_API_KEY"]


def test_behaviour_defaults_match_the_spec(monkeypatch: pytest.MonkeyPatch) -> None:
    """Values from Apêndice A of doc/spec.md. Behaviour may default; secrets may not."""
    _env(monkeypatch)
    settings = _build()

    assert settings.session_idle_timeout_min == 90
    assert settings.session_max_duration_min == 240
    assert settings.debounce_window_s == 10
    assert settings.interrupt_ttl_min == 20
    assert settings.ack_confidence_threshold == 0.85


def test_behaviour_defaults_are_overridable(monkeypatch: pytest.MonkeyPatch) -> None:
    _env(monkeypatch, DEBOUNCE_WINDOW_S="4", ACK_CONFIDENCE_THRESHOLD="0.9")
    settings = _build()

    assert settings.debounce_window_s == 4
    assert settings.ack_confidence_threshold == 0.9


def test_encryption_key_must_be_32_bytes(monkeypatch: pytest.MonkeyPatch) -> None:
    """AES-256-GCM needs exactly 32 bytes. A short key must fail at startup,
    not at the first write to an encrypted column."""
    _env(monkeypatch, FITTRACK_ENCRYPTION_KEY="dG9vLXNob3J0")  # b"too-short"

    with pytest.raises(ValidationError, match=r"32 bytes"):
        _build()


def test_encryption_key_must_be_valid_base64(monkeypatch: pytest.MonkeyPatch) -> None:
    _env(monkeypatch, FITTRACK_ENCRYPTION_KEY="not base64 at all !!")

    with pytest.raises(ValidationError, match=r"base64"):
        _build()


def test_database_url_must_be_async_driver(monkeypatch: pytest.MonkeyPatch) -> None:
    """A sync driver silently blocks the event loop under load."""
    _env(monkeypatch, DATABASE_URL="postgresql://u:p@localhost:5432/db")

    with pytest.raises(ValidationError, match=r"asyncpg"):
        _build()


def test_blank_credential_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """.env.example ships every variable empty. A blank token must not start
    the app and defer the failure to the first API call."""
    _env(monkeypatch, WABA_TOKEN="")

    with pytest.raises(ValidationError, match=r"must not be empty"):
        _build()


def test_whitespace_only_credential_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _env(monkeypatch, ANTHROPIC_API_KEY="   ")

    with pytest.raises(ValidationError, match=r"must not be empty"):
        _build()


def test_database_password_does_not_leak(monkeypatch: pytest.MonkeyPatch) -> None:
    """A connection URI carries a password. Plain str would print it in any
    traceback that renders the settings object."""
    _env(monkeypatch)
    settings = _build()

    assert "sup3rs3cr3t" not in repr(settings)
    assert "sup3rs3cr3t" not in str(settings)
    assert settings.database_url.get_secret_value().endswith("/db")


@pytest.mark.parametrize(
    "url",
    [
        "postgresql://u:p@h:5432/db?note=+asyncpg",  # substring present, sync driver
        "postgresql+asyncpgx://u:p@h:5432/db",  # typo, not a real driver
        "postgresql+psycopg://u:p@h:5432/db",  # explicit sync driver
        "mysql+asyncpg://u:p@h/db",  # right driver, wrong database
    ],
)
def test_rejects_urls_that_only_look_async(monkeypatch: pytest.MonkeyPatch, url: str) -> None:
    """A substring check would accept the first two."""
    _env(monkeypatch, DATABASE_URL=url)

    with pytest.raises(ValidationError, match=r"postgresql\+asyncpg"):
        _build()
