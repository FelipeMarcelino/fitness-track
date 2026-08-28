"""Configuration is validated at startup, or it is not validated at all.

Everything in `settings.py` and `config.py` is written to fail at boot. That is
worth nothing if no entry point calls it: the service starts, reports healthy,
and the missing credential surfaces on the first real request instead.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from fittrack.startup import StartupError, validate_startup

CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"


def environment(**overrides: str) -> dict[str, str]:
    import base64
    import json

    base = {
        "DATABASE_URL": "postgresql+asyncpg://fittrack_runtime:p@postgres:5432/f?sslmode=verify-full",
        "REDIS_URL": "rediss://:p@redis:6379/0",
        "QDRANT_URL": "https://qdrant:6333",
        "FITTRACK_CHANNELS": "",
        "FITTRACK_ENCRYPTION_KEYS": json.dumps({"1": base64.b64encode(b"A" * 32).decode()}),
        "FITTRACK_ACTIVE_KEY_VERSION": "1",
        "FITTRACK_IDENTITY_PEPPER": "a-startup-pepper-of-sufficient-length",
        "FITTRACK_CONFIG_DIR": str(CONFIG_DIR),
    }
    base.update(overrides)
    return base


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    for name in list(os.environ):
        if name.startswith(("FITTRACK_", "DATABASE_", "REDIS_", "QDRANT_", "TELEGRAM_", "WABA_")):
            monkeypatch.delenv(name, raising=False)
    return monkeypatch


def test_a_valid_environment_starts(clean_env: pytest.MonkeyPatch) -> None:
    for name, value in environment().items():
        clean_env.setenv(name, value)
    settings, config = validate_startup(env_file=None)
    assert settings.active_key_version == 1
    assert config.quota.plans["free"].llm_usd_month > 0


def test_a_missing_variable_stops_the_service(clean_env: pytest.MonkeyPatch) -> None:
    values = environment()
    del values["DATABASE_URL"]
    for name, value in values.items():
        clean_env.setenv(name, value)
    with pytest.raises(StartupError, match="environment"):
        validate_startup(env_file=None)


def test_a_broken_keyring_stops_the_service(clean_env: pytest.MonkeyPatch) -> None:
    for name, value in environment(FITTRACK_ENCRYPTION_KEYS="{}").items():
        clean_env.setenv(name, value)
    with pytest.raises(StartupError):
        validate_startup(env_file=None)


def test_a_channel_without_credentials_stops_the_service(
    clean_env: pytest.MonkeyPatch,
) -> None:
    for name, value in environment(FITTRACK_CHANNELS="telegram").items():
        clean_env.setenv(name, value)
    with pytest.raises(StartupError, match="TELEGRAM_BOT_TOKEN"):
        validate_startup(env_file=None)


def test_broken_yaml_stops_the_service(clean_env: pytest.MonkeyPatch, tmp_path: Path) -> None:
    (tmp_path / "models.yaml").write_text("roles: [unclosed\n", encoding="utf-8")
    for name, value in environment(FITTRACK_CONFIG_DIR=str(tmp_path)).items():
        clean_env.setenv(name, value)
    with pytest.raises(StartupError, match="configuration"):
        validate_startup(env_file=None)


def test_the_error_does_not_quote_a_secret(clean_env: pytest.MonkeyPatch) -> None:
    """A startup failure is the most-read log line there is."""
    secret = "hunter2-do-not-print"
    values = environment(
        DATABASE_URL=f"postgresql+asyncpg://u:{secret}@postgres:5432/f?sslmode=disable"
    )
    for name, value in values.items():
        clean_env.setenv(name, value)
    with pytest.raises(StartupError) as raised:
        validate_startup(env_file=None)
    assert secret not in str(raised.value)


def test_no_startup_error_carries_the_input_that_failed(
    clean_env: pytest.MonkeyPatch,
) -> None:
    """Pydantic reports the input alongside the message, and the input is the env.

    A boot failure is the most-read line in any log, and here the input is a
    mapping that includes every DSN — passwords and all. The message is
    rebuilt from location and reason only.
    """
    secret = "hunter2-do-not-print-me-ever"
    values = environment(
        DATABASE_URL=f"postgresql+asyncpg://fittrack_runtime:{secret}@postgres:5432/f?sslmode=verify-full",
        FITTRACK_ENCRYPTION_KEYS="{}",
    )
    for name, value in values.items():
        clean_env.setenv(name, value)
    with pytest.raises(StartupError) as raised:
        validate_startup(env_file=None)
    message = str(raised.value)
    assert secret not in message
    assert "input_value" not in message
    assert "key version" in message, "the reason must survive the redaction"


def test_an_uncaught_startup_failure_prints_no_secret(tmp_path: Path) -> None:
    """`raise ... from error` would have leaked everything the redaction removed.

    Chaining keeps the ValidationError as the explicit cause, and an uncaught
    traceback prints a cause in full — including the input pydantic rejected,
    which is the whole environment with every password in it. Checked in a real
    subprocess, because the leak is a property of traceback printing.
    """
    import os
    import subprocess
    import sys

    secret = "hunter2-must-not-be-printed"
    child = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("FITTRACK_", "DATABASE_", "REDIS_", "QDRANT_"))
    }
    child |= environment(
        DATABASE_URL=f"postgresql+asyncpg://fittrack_runtime:{secret}@postgres:5432/f?sslmode=verify-full",
        FITTRACK_ENCRYPTION_KEYS="{}",
    )
    child["PYTHONPATH"] = str(Path(__file__).resolve().parents[2] / "src")

    result = subprocess.run(
        [sys.executable, "-c", "from fittrack.startup import startup; startup('p', env_file=None)"],
        capture_output=True,
        text=True,
        env=child,
    )
    assert result.returncode != 0
    assert secret not in result.stdout + result.stderr
