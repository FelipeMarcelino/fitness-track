#!/usr/bin/env python3
"""Turn `.env.example` into a `.env` the local stack can boot with.

The committed template carries `change-me` placeholders, which is correct for a
template and useless for a service: Langfuse rejects an encryption key that is
not 64 hex characters, and Postgres and Redis need the password in the
connection URL to match the one the container was started with. Filling those
by hand is where a local setup usually breaks, so it is done here instead.

Only *local infrastructure* credentials are generated. Provider keys stay empty
— those belong to the operator, and inventing one would only hide that it is
missing.
"""

from __future__ import annotations

import argparse
import secrets
import sys
from collections.abc import Callable
from pathlib import Path

PLACEHOLDER = "change-me"

# Langfuse validates this one: 256 bits as 64 hex characters, nothing else.
HEX_KEYS = frozenset({"LANGFUSE_ENCRYPTION_KEY"})

# URL-safe on purpose: these end up inside a connection URL, where a `@` or a
# `:` would silently change what is being parsed.
SecretFactory = Callable[[str], str]


def _default_factory(name: str) -> str:
    if name in HEX_KEYS:
        return secrets.token_hex(32)
    return secrets.token_urlsafe(24)


def _split(line: str) -> tuple[str, str] | None:
    if "=" not in line or line.lstrip().startswith("#"):
        return None
    name, _, value = line.partition("=")
    return name.strip(), value.strip()


def render_env(template: str, secret: SecretFactory = _default_factory) -> str:
    """Fill the placeholders of a template, then rebuild the URLs that use them."""
    values: dict[str, str] = {}
    for line in template.splitlines():
        parsed = _split(line)
        if parsed is None:
            continue
        name, value = parsed
        values[name] = secret(name) if value == PLACEHOLDER else value

    user = values.get("POSTGRES_USER", "fittrack")
    database = values.get("POSTGRES_DB", "fittrack")
    pg_password = values.get("POSTGRES_PASSWORD", "")
    redis_password = values.get("REDIS_PASSWORD", "")

    # Derived, never edited by hand: a URL that disagrees with the password the
    # container booted with fails at the first connection and nowhere earlier.
    values["DATABASE_URL"] = (
        f"postgresql+asyncpg://{user}:{pg_password}@postgres:5432/{database}"
        "?sslmode=verify-full&sslrootcert=/certs/ca.crt"
    )
    values["LANGFUSE_DATABASE_URL"] = f"postgresql://{user}:{pg_password}@postgres:5432/langfuse"
    values["REDIS_URL"] = f"rediss://:{redis_password}@redis:6379/0"

    out = []
    for line in template.splitlines():
        parsed = _split(line)
        out.append(line if parsed is None else f"{parsed[0]}={values[parsed[0]]}")
    return "\n".join(out) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--template", type=Path, default=Path(".env.example"))
    parser.add_argument("--output", type=Path, default=Path(".env"))
    parser.add_argument("--force", action="store_true", help="overwrite an existing .env")
    args = parser.parse_args(argv)

    if args.output.exists() and not args.force:
        print(f"{args.output} already exists; pass --force to regenerate.")
        return 0

    args.output.write_text(render_env(args.template.read_text(encoding="utf-8")), encoding="utf-8")
    args.output.chmod(0o600)
    print(f"wrote {args.output} with generated local credentials (mode 600).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
