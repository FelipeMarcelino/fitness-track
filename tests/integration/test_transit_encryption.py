"""The transit layer of spec 22.1, proved rather than declared.

Each store is checked three ways: a verified connection works, a plaintext one
does not, and a connection trusting the wrong CA does not either. The third is
the one that matters — enabling TLS without verifying the certificate buys
nothing against an attacker who can answer on the port.
"""

from __future__ import annotations

import ssl
from pathlib import Path

import asyncpg
import httpx
import pytest
import redis.asyncio as redis
from redis.exceptions import ConnectionError as RedisConnectionError

from tests.integration.conftest import HOST

QDRANT_HEALTH = f"https://{HOST['qdrant']}:6333/healthz"


# --------------------------------------------------------------------------- #
# Postgres
# --------------------------------------------------------------------------- #


async def test_postgres_accepts_a_verified_connection(
    postgres_dsn: str, trusting_ssl_context: ssl.SSLContext
) -> None:
    connection = await asyncpg.connect(postgres_dsn, ssl=trusting_ssl_context)
    try:
        assert await connection.fetchval("SELECT 1") == 1
        encrypted = await connection.fetchval(
            "SELECT ssl FROM pg_stat_ssl WHERE pid = pg_backend_pid()"
        )
        assert encrypted, "the session reports itself as unencrypted"
    finally:
        await connection.close()


async def test_postgres_refuses_a_plaintext_connection(postgres_dsn: str) -> None:
    """`hostssl` in pg_hba is what makes this fail; `ssl=on` alone would not."""
    with pytest.raises((asyncpg.InvalidAuthorizationSpecificationError, OSError)):
        await asyncpg.connect(postgres_dsn, ssl=False)


async def test_postgres_refuses_an_untrusted_certificate(
    postgres_dsn: str, foreign_ssl_context: ssl.SSLContext
) -> None:
    with pytest.raises(ssl.SSLError):
        await asyncpg.connect(postgres_dsn, ssl=foreign_ssl_context)


# --------------------------------------------------------------------------- #
# Redis
# --------------------------------------------------------------------------- #


def _redis(password: str, ca: Path | None) -> redis.Redis:
    if ca is None:
        return redis.Redis(host=HOST["redis"], port=6379, password=password, ssl=False)
    return redis.Redis(
        host=HOST["redis"],
        port=6379,
        password=password,
        ssl=True,
        ssl_cert_reqs="required",
        ssl_ca_certs=str(ca),
        ssl_check_hostname=True,
    )


async def test_redis_accepts_a_verified_connection(redis_password: str, ca_file: Path) -> None:
    client = _redis(redis_password, ca_file)
    try:
        assert await client.ping()
    finally:
        await client.aclose()


async def test_redis_has_no_plaintext_listener(redis_password: str) -> None:
    """`--port 0` removes the cleartext port outright, rather than deprioritising it."""
    client = _redis(redis_password, ca=None)
    try:
        with pytest.raises((RedisConnectionError, OSError)):
            await client.ping()
    finally:
        await client.aclose()


async def test_redis_refuses_an_untrusted_certificate(
    redis_password: str, foreign_ca_file: Path
) -> None:
    client = _redis(redis_password, foreign_ca_file)
    try:
        with pytest.raises((RedisConnectionError, ssl.SSLError, OSError)):
            await client.ping()
    finally:
        await client.aclose()


# --------------------------------------------------------------------------- #
# Qdrant
# --------------------------------------------------------------------------- #


async def test_qdrant_accepts_a_verified_connection(
    env: dict[str, str], trusting_ssl_context: ssl.SSLContext
) -> None:
    async with httpx.AsyncClient(verify=trusting_ssl_context) as client:
        response = await client.get(
            QDRANT_HEALTH, headers={"api-key": env.get("QDRANT_API_KEY", "")}
        )
    assert response.status_code == 200


async def test_qdrant_has_no_plaintext_listener() -> None:
    async with httpx.AsyncClient() as client:
        with pytest.raises(httpx.HTTPError):
            await client.get(f"http://{HOST['qdrant']}:6333/healthz", timeout=5.0)


async def test_qdrant_refuses_an_untrusted_certificate(
    foreign_ssl_context: ssl.SSLContext,
) -> None:
    async with httpx.AsyncClient(verify=foreign_ssl_context) as client:
        with pytest.raises(httpx.ConnectError):
            await client.get(QDRANT_HEALTH, timeout=5.0)
