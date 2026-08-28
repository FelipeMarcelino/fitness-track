"""The check every service runs before it serves anything.

`settings.py` and `config.py` are written so that an invalid deployment fails at
boot. That is worth nothing unless something calls them: without this, a service
with no `DATABASE_URL`, a malformed keyring or broken YAML starts, reports
healthy, and surfaces the problem on the first real request — which is the
incident the whole design was meant to avoid.

So it is called from `main.py`, `worker.py` and `scheduler.py`, and it is the
first thing each of them does.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import ValidationError

from fittrack.config import Config, ConfigError, load_config
from fittrack.settings import Settings, get_settings


class StartupError(RuntimeError):
    """The deployment is not viable. Raised before anything opens a connection."""


def _redacted(error: ValidationError) -> str:
    """A validation failure as location and reason, and nothing else.

    Pydantic reports the offending *input* alongside each message. For a model
    validator the input is the whole settings mapping — every DSN, passwords
    included. A boot failure is the most-read line in any log, so the message is
    rebuilt rather than formatted.
    """
    lines = []
    for detail in error.errors(include_url=False, include_input=False, include_context=False):
        location = ".".join(str(part) for part in detail["loc"]) or "(model)"
        lines.append(f"  {location}: {detail['msg']}")
    return "\n".join(lines)


# Sentinel: `None` is a meaningful value for pydantic-settings (it means "read
# no file"), so it cannot double as "not specified".
_DEFAULT_ENV_FILE = ".env"


def validate_startup(env_file: str | None = _DEFAULT_ENV_FILE) -> tuple[Settings, Config]:
    """Load and validate the environment and the versioned configuration.

    `env_file=None` reads only the process environment, which is what a test —
    or a container that injects everything explicitly — wants.
    """
    try:
        settings = Settings(_env_file=env_file)
    except ValidationError as error:
        # `from None`, deliberately. Chaining keeps the ValidationError as the
        # explicit cause, and an uncaught traceback prints a cause in full —
        # including the input pydantic rejected, which is the whole environment
        # with every password in it. Redacting the message and then attaching
        # the unredacted original would defeat the redaction entirely.
        raise StartupError(f"invalid environment:\n{_redacted(error)}") from None

    try:
        config = load_config(Path(settings.fittrack_config_dir))
    except ConfigError as error:
        raise StartupError(f"invalid configuration: {error}") from None

    return settings, config


def startup(service: str, env_file: str | None = _DEFAULT_ENV_FILE) -> tuple[Settings, Config]:
    """Validate, and say so. The one line worth having in a boot log."""
    settings, config = validate_startup(env_file)
    print(  # noqa: T201 — before logging is configured, this is the only channel
        f"{service}: configuration valid "
        f"(channels={list(settings.channels)}, roles={len(config.models.roles)})"
    )
    return settings, config


__all__ = ["StartupError", "get_settings", "startup", "validate_startup"]
