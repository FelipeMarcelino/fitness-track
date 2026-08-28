"""Typed configuration and the secret boundary (spec sections 7.2, 19.3, 22, Apêndice A).

Two properties are load-bearing here and each has its own block below:

1. An invalid environment fails at boot, before anything opens a connection. A
   missing credential discovered on the first webhook is an incident; the same
   one discovered at import is a deployment that never started.
2. A secret never reaches a repr, a serialisation or a log. Section 20.2 already
   keeps user content out of Datadog; this keeps the keys out of everything.
"""

from __future__ import annotations

import base64
import json
import os

import pytest
from pydantic import ValidationError

from fittrack.settings import Settings

KEY_A = base64.b64encode(b"A" * 32).decode()
KEY_B = base64.b64encode(b"B" * 32).decode()
PEPPER = "an-independent-pepper"


def env(**overrides: str) -> dict[str, str]:
    """A minimal valid environment; each test breaks exactly one thing."""
    base = {
        "DATABASE_URL": "postgresql+asyncpg://fittrack_runtime:p@postgres:5432/f?sslmode=verify-full",
        "REDIS_URL": "rediss://:p@redis:6379/0",
        "QDRANT_URL": "https://qdrant:6333",
        "QDRANT_API_KEY": "qdrant-key",
        "FITTRACK_CHANNELS": "",
        "FITTRACK_ENCRYPTION_KEYS": json.dumps({"1": KEY_A}),
        "FITTRACK_ACTIVE_KEY_VERSION": "1",
        "FITTRACK_IDENTITY_PEPPER": PEPPER,
    }
    base.update(overrides)
    return base


# Prefixes this configuration owns. A stray one in the developer's shell would
# otherwise leak into a test and make it pass for the wrong reason.
OWNED_PREFIXES = (
    "FITTRACK_",
    "TELEGRAM_",
    "WABA_",
    "DATABASE_",
    "MIGRATION_",
    "REDIS_",
    "QDRANT_",
    "GROQ_",
    "ANTHROPIC_",
    "OPENAI_",
    "XAI_",
    "LANGFUSE_",
    "OTEL_",
    "MERCADOPAGO_",
    "SESSION_",
    "DEBOUNCE_",
    "INTERRUPT_",
    "ACK_",
    "CHANNEL_",
    "GRAPH_",
    "CHECKPOINT_",
    "POSTGRES_",
)


def build(**overrides: str) -> Settings:
    """Construct through the environment, which is how the process will do it."""
    with pytest.MonkeyPatch.context() as patch:
        for name in list(os.environ):
            if name.startswith(OWNED_PREFIXES):
                patch.delenv(name, raising=False)
        for name, value in env(**overrides).items():
            patch.setenv(name, value)
        return Settings(_env_file=None)


# --------------------------------------------------------------------------- #
# Defaults and overrides
# --------------------------------------------------------------------------- #


def test_a_minimal_environment_is_valid() -> None:
    settings = build()
    assert settings.active_key_version == 1
    assert settings.channels == ()


def test_behaviour_defaults_match_the_appendix() -> None:
    settings = build()
    assert settings.session_idle_timeout_min == 90
    assert settings.session_max_duration_min == 240
    assert settings.debounce_window_s == 10
    assert settings.interrupt_ttl_min == 20
    assert settings.ack_confidence_threshold == 0.85
    assert settings.graph_recursion_limit == 40


def test_the_environment_overrides_a_default() -> None:
    assert build(DEBOUNCE_WINDOW_S="30").debounce_window_s == 30


def test_settings_are_frozen() -> None:
    """One process, one configuration: a mutable setting is a heisenbug."""
    settings = build()
    with pytest.raises(ValidationError):
        settings.debounce_window_s = 5  # type: ignore[misc]


@pytest.mark.parametrize("missing", ["DATABASE_URL", "REDIS_URL", "QDRANT_URL"])
def test_a_missing_required_variable_fails(missing: str) -> None:
    with pytest.MonkeyPatch.context() as patch:
        for name in list(os.environ):
            if name.startswith(OWNED_PREFIXES):
                patch.delenv(name, raising=False)
        for name, value in env().items():
            if name != missing:
                patch.setenv(name, value)
        with pytest.raises(ValidationError, match=missing.lower()):
            Settings(_env_file=None)


def test_an_out_of_range_threshold_fails() -> None:
    with pytest.raises(ValidationError):
        build(ACK_CONFIDENCE_THRESHOLD="1.5")


# --------------------------------------------------------------------------- #
# Transit encryption (section 22.1)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("variable", ["DATABASE_URL", "MIGRATION_DATABASE_URL"])
def test_a_postgres_url_that_does_not_verify_is_rejected(variable: str) -> None:
    """`verify-ca` would accept any certificate this CA signed — every service."""
    with pytest.raises(ValidationError, match="verify-full"):
        build(**{variable: "postgresql+asyncpg://u:p@postgres:5432/f?sslmode=verify-ca"})


def test_the_application_does_not_need_the_owner_credential() -> None:
    """Handing the owner DSN to the ingress would undo the separation entirely."""
    assert build().migration_database_url is None


def test_the_application_must_not_connect_as_the_migration_principal() -> None:
    """Spec 19.1, and the failure it prevents is silent rather than loud.

    The owner bypasses row level security unless FORCE is set, and a superuser
    or BYPASSRLS role bypasses it regardless. Pointing both URLs at the same
    principal leaves every policy in place and never evaluated.
    """
    same = "postgresql+asyncpg://fittrack_runtime:p@postgres:5432/f?sslmode=verify-full"
    with pytest.raises(ValidationError, match="same principal"):
        build(DATABASE_URL=same, MIGRATION_DATABASE_URL=same)


def test_the_application_url_must_name_the_runtime_principal() -> None:
    """The check that runs where it matters.

    Application containers deliberately have no MIGRATION_DATABASE_URL, so a
    comparison between the two would never fire there — and a production URL
    naming the owner would pass validation and bypass every policy.
    """
    with pytest.raises(ValidationError, match="fittrack_runtime"):
        build(DATABASE_URL="postgresql+asyncpg://fittrack:p@postgres:5432/f?sslmode=verify-full")


def test_two_distinct_principals_are_accepted() -> None:
    settings = build(
        MIGRATION_DATABASE_URL="postgresql+asyncpg://owner:p@postgres:5432/f?sslmode=verify-full"
    )
    assert settings.migration_database_url is not None


def test_a_plaintext_redis_url_is_rejected() -> None:
    with pytest.raises(ValidationError, match="rediss"):
        build(REDIS_URL="redis://:p@redis:6379/0")


def test_a_plaintext_qdrant_url_is_rejected() -> None:
    with pytest.raises(ValidationError, match="https"):
        build(QDRANT_URL="http://qdrant:6333")


# --------------------------------------------------------------------------- #
# The cryptographic contract (section 22.2)
# --------------------------------------------------------------------------- #


def test_the_keyring_loads() -> None:
    settings = build(FITTRACK_ENCRYPTION_KEYS=json.dumps({"1": KEY_A, "2": KEY_B}))
    assert set(settings.encryption_keys) == {1, 2}


def test_the_active_version_must_exist_in_the_keyring() -> None:
    with pytest.raises(ValidationError, match="not in the keyring"):
        build(FITTRACK_ENCRYPTION_KEYS=json.dumps({"1": KEY_A}), FITTRACK_ACTIVE_KEY_VERSION="2")


@pytest.mark.parametrize("version", ["0", "-1", "32768", "70000"])
def test_a_version_outside_the_smallint_range_is_rejected(version: str) -> None:
    """Two bytes in the blob header and a positive SMALLINT column: 1..32767."""
    with pytest.raises(ValidationError):
        build(FITTRACK_ENCRYPTION_KEYS=json.dumps({version: KEY_A}))


def test_a_key_that_is_not_32_bytes_is_rejected() -> None:
    short = base64.b64encode(b"A" * 16).decode()
    with pytest.raises(ValidationError, match="32 bytes"):
        build(FITTRACK_ENCRYPTION_KEYS=json.dumps({"1": short}))


def test_a_key_that_is_not_base64_is_rejected() -> None:
    with pytest.raises(ValidationError, match="base64"):
        build(FITTRACK_ENCRYPTION_KEYS=json.dumps({"1": "not base64 at all!"}))


def test_an_empty_keyring_is_rejected() -> None:
    with pytest.raises(ValidationError):
        build(FITTRACK_ENCRYPTION_KEYS="{}")


def test_the_pepper_must_be_independent_of_the_keyring() -> None:
    """They rotate separately; sharing material would couple the two procedures."""
    with pytest.raises(ValidationError, match="independent"):
        build(FITTRACK_IDENTITY_PEPPER=KEY_A)


def test_an_empty_pepper_is_rejected() -> None:
    with pytest.raises(ValidationError):
        build(FITTRACK_IDENTITY_PEPPER="")


# --------------------------------------------------------------------------- #
# Channels: listing one without its credentials fails at boot
# --------------------------------------------------------------------------- #


def test_telegram_requires_a_bot_token() -> None:
    with pytest.raises(ValidationError, match="TELEGRAM_BOT_TOKEN"):
        build(FITTRACK_CHANNELS="telegram")


def test_telegram_in_webhook_mode_requires_a_secret_and_a_url() -> None:
    with pytest.raises(ValidationError, match="TELEGRAM_WEBHOOK_SECRET"):
        build(FITTRACK_CHANNELS="telegram", TELEGRAM_BOT_TOKEN="t", TELEGRAM_MODE="webhook")


def test_telegram_in_polling_mode_needs_only_the_token() -> None:
    settings = build(FITTRACK_CHANNELS="telegram", TELEGRAM_BOT_TOKEN="t", TELEGRAM_MODE="polling")
    assert settings.channels == ("telegram",)


def test_the_webhook_secret_must_match_the_alphabet_telegram_accepts() -> None:
    with pytest.raises(ValidationError, match="alphabet"):
        build(
            FITTRACK_CHANNELS="telegram",
            TELEGRAM_BOT_TOKEN="t",
            TELEGRAM_MODE="webhook",
            TELEGRAM_WEBHOOK_SECRET="has spaces and $",
            TELEGRAM_WEBHOOK_URL="https://example.com/webhook/telegram",
        )


def test_whatsapp_requires_its_four_credentials() -> None:
    with pytest.raises(ValidationError, match="WABA_"):
        build(FITTRACK_CHANNELS="whatsapp")


def test_an_unknown_channel_is_rejected() -> None:
    with pytest.raises(ValidationError):
        build(FITTRACK_CHANNELS="telegram,signal")


def test_both_channels_can_be_enabled_together() -> None:
    settings = build(
        FITTRACK_CHANNELS="telegram,whatsapp",
        TELEGRAM_BOT_TOKEN="t",
        TELEGRAM_MODE="polling",
        WABA_PHONE_NUMBER_ID="1",
        WABA_TOKEN="2",
        WABA_APP_SECRET="3",
        WABA_VERIFY_TOKEN="4",
    )
    assert settings.channels == ("telegram", "whatsapp")


# --------------------------------------------------------------------------- #
# The secret boundary
# --------------------------------------------------------------------------- #

SECRET_VALUE = "sk-do-not-print-me"


@pytest.fixture
def loaded() -> Settings:
    return build(
        ANTHROPIC_API_KEY=SECRET_VALUE,
        GROQ_API_KEY=SECRET_VALUE,
        FITTRACK_CHANNELS="telegram",
        TELEGRAM_BOT_TOKEN=SECRET_VALUE,
        TELEGRAM_MODE="polling",
    )


def test_a_secret_does_not_appear_in_repr(loaded: Settings) -> None:
    assert SECRET_VALUE not in repr(loaded)
    assert SECRET_VALUE not in str(loaded)


def test_a_secret_does_not_appear_in_a_serialisation(loaded: Settings) -> None:
    assert SECRET_VALUE not in loaded.model_dump_json()
    assert SECRET_VALUE not in str(loaded.model_dump())


def test_the_keyring_does_not_appear_in_a_serialisation() -> None:
    settings = build()
    assert KEY_A not in repr(settings)
    assert KEY_A not in settings.model_dump_json()


def test_the_pepper_does_not_appear_in_a_serialisation() -> None:
    settings = build()
    assert PEPPER not in repr(settings)
    assert PEPPER not in settings.model_dump_json()


def test_a_url_password_does_not_appear_in_repr() -> None:
    """The DSN carries the Postgres password, so the whole URL is a secret."""
    settings = build(
        DATABASE_URL="postgresql+asyncpg://fittrack_runtime:hunter2@postgres:5432/f"
        "?sslmode=verify-full"
    )
    assert "hunter2" not in repr(settings)
    assert "hunter2" not in settings.model_dump_json()


def test_a_validation_error_does_not_quote_the_secret_it_rejected() -> None:
    """The usual leak: a message that helpfully shows the value that failed."""
    with pytest.raises(ValidationError) as raised:
        build(FITTRACK_ENCRYPTION_KEYS=json.dumps({"1": "not-base64-" + SECRET_VALUE}))
    assert SECRET_VALUE not in str(raised.value)


def test_the_secret_is_reachable_when_it_is_actually_needed(loaded: Settings) -> None:
    assert loaded.anthropic_api_key is not None
    assert loaded.anthropic_api_key.get_secret_value() == SECRET_VALUE


def test_a_rejected_dsn_is_not_quoted_back(  # the DSN carries the Postgres password
) -> None:
    with pytest.raises(ValidationError) as raised:
        build(
            DATABASE_URL="postgresql+asyncpg://fittrack_runtime:hunter2@postgres:5432/f?sslmode=require"
        )
    assert "hunter2" not in str(raised.value)


def test_a_rejected_redis_url_is_not_quoted_back() -> None:
    with pytest.raises(ValidationError) as raised:
        build(REDIS_URL="redis://:hunter2@redis:6379/0")
    assert "hunter2" not in str(raised.value)


def test_a_rejected_pepper_is_not_quoted_back() -> None:
    with pytest.raises(ValidationError) as raised:
        build(FITTRACK_IDENTITY_PEPPER=KEY_A)
    assert KEY_A not in str(raised.value)


def test_an_empty_credential_counts_as_missing() -> None:
    """`TELEGRAM_BOT_TOKEN=` in a .env is an unset token, not an empty one.

    Treating it as present is how a channel gets enabled with no way to talk to
    it, and the failure then surfaces on the first webhook instead of at boot.
    """
    with pytest.raises(ValidationError, match="TELEGRAM_BOT_TOKEN"):
        build(FITTRACK_CHANNELS="telegram", TELEGRAM_BOT_TOKEN="", TELEGRAM_MODE="polling")


def test_a_whitespace_only_credential_counts_as_missing() -> None:
    with pytest.raises(ValidationError, match="TELEGRAM_BOT_TOKEN"):
        build(FITTRACK_CHANNELS="telegram", TELEGRAM_BOT_TOKEN="   ", TELEGRAM_MODE="polling")


def test_an_empty_optional_secret_reads_as_absent() -> None:
    assert build(ANTHROPIC_API_KEY="").anthropic_api_key is None


# --------------------------------------------------------------------------- #
# The checks that a substring match would have let through
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "dsn",
    [
        # The literal appears, but not as the effective parameter.
        "postgresql+asyncpg://u:sslmode=verify-full@postgres:5432/f?sslmode=disable",
        "postgresql+asyncpg://u:p@postgres:5432/sslmode=verify-full?sslmode=disable",
        "postgresql+asyncpg://u:p@postgres:5432/f?note=sslmode=verify-full&sslmode=require",
    ],
)
def test_verify_full_must_be_the_effective_parameter(dsn: str) -> None:
    """A substring check accepts a DSN that disables TLS outright."""
    with pytest.raises(ValidationError, match="verify-full"):
        build(DATABASE_URL=dsn)


def test_a_dsn_with_no_sslmode_at_all_is_rejected() -> None:
    with pytest.raises(ValidationError, match="verify-full"):
        build(DATABASE_URL="postgresql+asyncpg://u:p@postgres:5432/f")


def test_two_spellings_of_one_key_version_are_rejected() -> None:
    """`"1"` and `"01"` both normalise to 1, and one would silently win.

    A keyring that selects different master material for a version already in
    the database makes every ciphertext under the loser undecryptable.
    """
    duplicate = json.dumps({"1": KEY_A, "01": KEY_B})
    with pytest.raises(ValidationError, match="duplicate"):
        build(FITTRACK_ENCRYPTION_KEYS=duplicate)


def test_a_short_webhook_secret_is_rejected() -> None:
    """Spec 18.2 asks for 32 random bytes; it authenticates every update."""
    with pytest.raises(ValidationError, match="32"):
        build(
            FITTRACK_CHANNELS="telegram",
            TELEGRAM_BOT_TOKEN="t",
            TELEGRAM_MODE="webhook",
            TELEGRAM_WEBHOOK_SECRET="x",
            TELEGRAM_WEBHOOK_URL="https://example.com/webhook/telegram",
        )


def test_a_plaintext_webhook_url_is_rejected() -> None:
    """Telegram refuses to register a webhook that is not HTTPS (spec 3.1)."""
    with pytest.raises(ValidationError, match="https"):
        build(
            FITTRACK_CHANNELS="telegram",
            TELEGRAM_BOT_TOKEN="t",
            TELEGRAM_MODE="webhook",
            TELEGRAM_WEBHOOK_SECRET="a" * 43,
            TELEGRAM_WEBHOOK_URL="http://example.com/webhook/telegram",
        )


def test_a_well_formed_webhook_configuration_is_accepted() -> None:
    settings = build(
        FITTRACK_CHANNELS="telegram",
        TELEGRAM_BOT_TOKEN="t",
        TELEGRAM_MODE="webhook",
        TELEGRAM_WEBHOOK_SECRET="a" * 43,
        TELEGRAM_WEBHOOK_URL="https://example.com/webhook/telegram",
    )
    assert settings.channels == ("telegram",)


def test_the_pepper_may_not_be_the_key_material_in_another_spelling() -> None:
    """A pepper is used as raw bytes and a key is base64 of those same bytes.

    Two different strings, one set of bytes feeding both cryptographic
    operations — which is exactly what the independence rule forbids.
    """
    with pytest.raises(ValidationError, match="independent"):
        build(
            FITTRACK_ENCRYPTION_KEYS=json.dumps({"1": base64.b64encode(b"A" * 32).decode()}),
            FITTRACK_IDENTITY_PEPPER="A" * 32,
        )


@pytest.mark.parametrize(
    "variable", ["TELEGRAM_MODE", "DEBOUNCE_WINDOW_S", "ACK_CONFIDENCE_THRESHOLD"]
)
def test_an_empty_optional_variable_falls_back_to_its_default(variable: str) -> None:
    """Compose interpolates an unset `${VAR}` to an empty string, not to nothing.

    Without this, a deployment with Telegram disabled hands the enum `""` and
    every numeric setting `""`, and none of the services boot.
    """
    settings = build(**{variable: ""})
    assert settings.telegram_mode == "polling"
    assert settings.debounce_window_s == 10
    assert settings.ack_confidence_threshold == 0.85


def test_an_empty_required_variable_is_reported_as_missing() -> None:
    with pytest.raises(ValidationError, match="fittrack_encryption_keys"):
        build(FITTRACK_ENCRYPTION_KEYS="")


@pytest.mark.parametrize(
    "dsn",
    [
        "?sslmode=verify-full",
        "not-a-dsn?sslmode=verify-full",
        "postgresql+asyncpg://u:p@?sslmode=verify-full",
        "postgresql+asyncpg://u:p@postgres:5432/?sslmode=verify-full",
    ],
)
def test_a_malformed_dsn_is_rejected(dsn: str) -> None:
    """A URL with a good sslmode and nothing to connect to is not a valid DSN."""
    with pytest.raises(ValidationError):
        build(DATABASE_URL=dsn)
