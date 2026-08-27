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
import stat
import sys
from collections.abc import Callable
from pathlib import Path

PLACEHOLDER = "change-me"

# Langfuse validates this one: 256 bits as 64 hex characters, nothing else.
HEX_KEYS = frozenset({"LANGFUSE_ENCRYPTION_KEY"})

# Postgres reads these only when it initialises its data directory. Rotating one
# against a live volume leaves the server on the old password while every client
# starts using the new one, and the stack simply stops connecting — with an
# error that looks like anything but a credential change. So a plain
# regeneration keeps them, and rotating is something you ask for.
VOLUME_BOUND = frozenset(
    {
        "POSTGRES_PASSWORD",
        "LANGFUSE_DB_PASSWORD",
        # Not a database password, but bound to persisted data all the same:
        # Langfuse hashes API keys with the salt and encrypts stored credentials
        # with the key. Regenerating either leaves rows that can no longer be
        # validated or decrypted.
        "LANGFUSE_SALT",
        "LANGFUSE_ENCRYPTION_KEY",
    }
)

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


def render_env(
    template: str,
    secret: SecretFactory = _default_factory,
    *,
    previous: dict[str, str] | None = None,
    rotate: bool = False,
) -> str:
    """Fill the placeholders of a template, then rebuild the URLs that use them.

    `previous` carries the values of an existing `.env`. Credentials baked into
    a Postgres volume are kept from it unless `rotate` is set, so regenerating
    the file does not quietly break a running stack.
    """
    previous = previous or {}
    values: dict[str, str] = {}
    for line in template.splitlines():
        parsed = _split(line)
        if parsed is None:
            continue
        name, value = parsed
        if value != PLACEHOLDER:
            values[name] = value
            continue
        kept = previous.get(name)
        values[name] = kept if kept and name in VOLUME_BOUND and not rotate else secret(name)

    # An operator's own edits are not the generator's to discard: a filled
    # provider key, an enabled channel, a tuned window. Anything that differs
    # from the committed template and is not derived below stays as it is.
    template_values = {
        parsed[0]: parsed[1]
        for parsed in (_split(line) for line in template.splitlines())
        if parsed is not None
    }
    for name, kept in previous.items():
        if (
            name in values
            and kept
            and kept != PLACEHOLDER
            and values[name] == template_values.get(name)
        ):
            values[name] = kept

    # Derived, never edited by hand and never preserved from a previous file.
    values.update(derived_urls(values))
    return (
        "\n".join(
            line if (parsed := _split(line)) is None else f"{parsed[0]}={values[parsed[0]]}"
            for line in template.splitlines()
        )
        + "\n"
    )


def derived_urls(values: dict[str, str]) -> dict[str, str]:
    """The connection URLs, rebuilt from the credentials beside them.

    Used to write them *and* to check them. A URL that disagrees with the
    password the container booted with fails at the first connection and nowhere
    earlier — and one still carrying `change-me` from a copied template fails the
    same way, several minutes after `make up` reported success.
    """
    user = values.get("POSTGRES_USER", "fittrack")
    database = values.get("POSTGRES_DB", "fittrack")
    pg_password = values.get("POSTGRES_PASSWORD", "")
    redis_password = values.get("REDIS_PASSWORD", "")
    langfuse_password = values.get("LANGFUSE_DB_PASSWORD", "")

    return {
        "DATABASE_URL": (
            f"postgresql+asyncpg://{user}:{pg_password}@postgres:5432/{database}"
            "?sslmode=verify-full&sslrootcert=/certs/ca.crt"
        ),
        # Langfuse speaks Prisma, whose parameter names differ from libpq's:
        # `sslaccept=strict` plus a root certificate is its verify-full.
        "LANGFUSE_DATABASE_URL": (
            f"postgresql://langfuse:{langfuse_password}@postgres:5432/langfuse"
            "?sslmode=require&sslaccept=strict&sslcert=/certs/ca.crt"
        ),
        "REDIS_URL": f"rediss://:{redis_password}@redis:6379/0",
    }


def _existing(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        parsed = _split(line)
        if parsed is not None:
            values[parsed[0]] = parsed[1]
    return values


def _restrict(path: Path) -> bool:
    """Make the file private. Reports whether it had to be changed."""
    if stat.S_IMODE(path.stat().st_mode) == 0o600:
        return False
    path.chmod(0o600)
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--template", type=Path, default=Path(".env.example"))
    parser.add_argument("--output", type=Path, default=Path(".env"))
    parser.add_argument("--force", action="store_true", help="overwrite an existing .env")
    parser.add_argument(
        "--rotate",
        action="store_true",
        help="also regenerate the credentials baked into the Postgres volume "
        "(needs `make reset` afterwards, or the stack stops connecting)",
    )
    args = parser.parse_args(argv)

    previous = _existing(args.output)

    if args.output.exists() and not args.force:
        # Even on this path: an operator who copied the template under a 022
        # umask left database, provider and encryption secrets world-readable.
        if _restrict(args.output):
            print(f"{args.output} already exists; tightened its mode to 600.")

        # A copied template is the common case, and accepting it here means the
        # stack fails several minutes later at Langfuse's key validation, with
        # an error that points nowhere near a placeholder. A file that predates
        # a new template variable is the same problem wearing a different hat:
        # Compose substitutes an empty value and the initdb script aborts.
        expected = {
            parsed[0]
            for parsed in (
                _split(line) for line in args.template.read_text(encoding="utf-8").splitlines()
            )
            if parsed is not None
        }
        # `in value`, not `==`: a copied template leaves `change-me` *inside* the
        # connection URLs, where an equality check never sees it.
        leftover = sorted(name for name, value in previous.items() if PLACEHOLDER in value)
        absent = sorted(expected - set(previous))
        # And a URL can be free of placeholders and still wrong — an edited
        # password without a rebuilt URL leaves clients authenticating with the
        # old one. Recomputing from the credentials in the same file catches it.
        stale = sorted(
            name
            for name, expected_value in derived_urls(previous).items()
            if name in previous and previous[name] != expected_value
        )
        if leftover or absent or stale:
            problems = []
            if leftover:
                problems.append(f"still holds placeholders: {', '.join(leftover)}")
            if absent:
                problems.append(f"is missing: {', '.join(absent)}")
            if stale:
                problems.append(f"has stale derived URLs: {', '.join(stale)}")
            print(
                f"{args.output} {' and '.join(problems)}.\n"
                f"Run `make env ARGS=--force` to complete it.",
                file=sys.stderr,
            )
            return 1

        print(f"{args.output} already exists; pass --force to regenerate.")
        return 0

    args.output.write_text(
        render_env(
            args.template.read_text(encoding="utf-8"), previous=previous, rotate=args.rotate
        ),
        encoding="utf-8",
    )
    args.output.chmod(0o600)
    print(f"wrote {args.output} with generated local credentials (mode 600).")

    if previous and args.rotate:
        print(
            "Rotated the credentials Postgres only reads at initdb. Run `make reset`: "
            "the existing volume still holds the old ones."
        )
    elif previous and (kept := sorted(VOLUME_BOUND & set(previous))):
        print(f"Kept {', '.join(kept)} so the existing volume keeps working.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
