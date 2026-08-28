"""Async engine and session factory.

Two principals, on purpose (spec 19.1):

- the **runtime** principal (`fittrack_runtime`) is what the application uses.
  It is `NOSUPERUSER NOBYPASSRLS` and owns nothing, because a superuser — or any
  role with `BYPASSRLS` — ignores row level security even with `FORCE`. Pointing
  `DATABASE_URL` at the owner would leave the policies in place and never
  evaluated, which is a silent failure rather than an error.
- the **owner** principal runs migrations and nothing else.
"""

from __future__ import annotations

import ssl
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from fittrack.settings import Settings, get_settings

# libpq's names, which is how a verified Postgres URL is written everywhere and
# what `.env.example` documents. asyncpg accepts neither: SQLAlchemy hands
# unknown query parameters straight to `asyncpg.connect()`, which rejects them.
SSL_PARAMETERS = ("sslmode", "sslrootcert")

# Spec 22.1 has no unverified hop. Accepting `require` — which encrypts but does
# not authenticate — would create one quietly, which is the failure the internal
# CA exists to prevent.
VERIFYING_MODES = {"verify-full": True, "verify-ca": False}


def split_ssl_arguments(dsn: str) -> tuple[str, dict[str, Any]]:
    """Split a libpq-style DSN into a URL asyncpg accepts and its connect args.

    Returns the URL without the SSL parameters, and `{"ssl": SSLContext}` when
    the DSN asked for verification. The intent stays where a human reads it —
    in the URL — and the translation happens once, here, so the two cannot
    drift.
    """
    parsed = urlsplit(dsn)
    query = parse_qsl(parsed.query, keep_blank_values=True)
    ssl_values = {name: value for name, value in query if name in SSL_PARAMETERS}
    if not ssl_values:
        return dsn, {}

    remaining = [(name, value) for name, value in query if name not in SSL_PARAMETERS]
    clean = urlunsplit(parsed._replace(query=urlencode(remaining)))

    mode = ssl_values.get("sslmode", "")
    if mode not in VERIFYING_MODES:
        raise ValueError(
            f"sslmode={mode!r} does not verify the server certificate; "
            "spec 22.1 has no unverified hop"
        )

    root = ssl_values.get("sslrootcert")
    if not root:
        raise ValueError(
            f"sslmode={mode} requires sslrootcert: the system trust "
            "store does not contain the internal CA"
        )
    if not Path(root).is_file():
        raise ValueError(f"sslrootcert points at {root}, which does not exist")

    context = ssl.create_default_context(cafile=root)
    context.check_hostname = VERIFYING_MODES[mode]
    context.verify_mode = ssl.CERT_REQUIRED
    return clean, {"ssl": context}


def create_engine(dsn: str, *, echo: bool = False) -> AsyncEngine:
    url, ssl_args = split_ssl_arguments(dsn)
    return create_async_engine(
        url,
        echo=echo,
        pool_pre_ping=True,
        connect_args={
            **ssl_args,
            "server_settings": {"application_name": "fittrack"},
        },
    )


@lru_cache(maxsize=1)
def get_engine(settings: Settings | None = None) -> AsyncEngine:
    """The application engine, connected as the runtime principal."""
    config = settings or get_settings()
    return create_engine(config.database_url.get_secret_value())


def session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


@asynccontextmanager
async def tenant_session(
    factory: async_sessionmaker[AsyncSession], tenant_id: int
) -> AsyncIterator[AsyncSession]:
    """A transaction scoped to one tenant.

    `SET LOCAL` is what makes row level security do anything: the policies read
    `app.tenant_id`, and a transaction that forgets to set it reads nothing
    rather than everything (spec 19.1). `LOCAL` ties the setting to the
    transaction, so a pooled connection cannot carry one tenant's context into
    another's query.
    """
    from sqlalchemy import text

    async with factory() as session, session.begin():
        await session.execute(
            text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
            {"tenant_id": str(tenant_id)},
        )
        yield session
