"""`make env` must produce a `.env` the stack can actually boot with.

A `change-me` placeholder is right for the committed template and wrong for a
running service: Langfuse, for one, refuses anything that is not 64 hex
characters. The renderer fills the local-only credentials and keeps the derived
URLs in agreement with them, which is the part a human gets wrong by hand.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from scripts.init_dev_env import PLACEHOLDER, render_env

TEMPLATE = Path(__file__).resolve().parents[2] / ".env.example"


def parse(text: str) -> dict[str, str]:
    return {
        line.split("=", 1)[0].strip(): line.split("=", 1)[1].strip()
        for line in text.splitlines()
        if "=" in line and not line.lstrip().startswith("#")
    }


@pytest.fixture(scope="module")
def rendered() -> dict[str, str]:
    return parse(render_env(TEMPLATE.read_text(encoding="utf-8")))


def test_no_placeholder_survives(rendered: dict[str, str]) -> None:
    leftover = {name for name, value in rendered.items() if PLACEHOLDER in value}
    assert not leftover


def test_the_langfuse_encryption_key_is_64_hex_characters(rendered: dict[str, str]) -> None:
    assert re.fullmatch(r"[0-9a-f]{64}", rendered["LANGFUSE_ENCRYPTION_KEY"])


def test_the_database_url_carries_the_generated_password(rendered: dict[str, str]) -> None:
    password = rendered["POSTGRES_PASSWORD"]
    assert password and PLACEHOLDER not in password
    assert f":{password}@postgres:5432" in rendered["DATABASE_URL"]
    assert f":{password}@postgres:5432" in rendered["LANGFUSE_DATABASE_URL"]


def test_the_database_url_keeps_verifying_the_certificate(rendered: dict[str, str]) -> None:
    assert "sslmode=verify-full" in rendered["DATABASE_URL"]
    assert "sslrootcert=/certs/ca.crt" in rendered["DATABASE_URL"]


def test_the_redis_url_carries_the_generated_password_over_tls(rendered: dict[str, str]) -> None:
    assert rendered["REDIS_URL"] == f"rediss://:{rendered['REDIS_PASSWORD']}@redis:6379/0"


def test_generated_credentials_are_url_safe(rendered: dict[str, str]) -> None:
    for name in ("POSTGRES_PASSWORD", "REDIS_PASSWORD"):
        assert re.fullmatch(r"[A-Za-z0-9_-]+", rendered[name]), name


def test_provider_credentials_stay_empty(rendered: dict[str, str]) -> None:
    """Only local infrastructure is generated; a real API key is the operator's."""
    for name in ("ANTHROPIC_API_KEY", "GROQ_API_KEY", "TELEGRAM_BOT_TOKEN"):
        assert rendered[name] == ""


def test_non_secret_settings_are_left_alone(rendered: dict[str, str]) -> None:
    assert rendered["FITTRACK_CHANNELS"] == "telegram"
    assert rendered["DEBOUNCE_WINDOW_S"] == "10"
    assert rendered["FITTRACK_ACTIVE_KEY_VERSION"] == "1"


def test_two_renders_do_not_share_a_secret() -> None:
    first = parse(render_env(TEMPLATE.read_text(encoding="utf-8")))
    second = parse(render_env(TEMPLATE.read_text(encoding="utf-8")))
    assert first["POSTGRES_PASSWORD"] != second["POSTGRES_PASSWORD"]


def test_comments_and_ordering_survive() -> None:
    template = TEMPLATE.read_text(encoding="utf-8")
    rendered = render_env(template)
    assert [line for line in template.splitlines() if line.startswith("#")] == [
        line for line in rendered.splitlines() if line.startswith("#")
    ]
    assert [line.split("=", 1)[0] for line in template.splitlines() if "=" in line] == [
        line.split("=", 1)[0] for line in rendered.splitlines() if "=" in line
    ]
