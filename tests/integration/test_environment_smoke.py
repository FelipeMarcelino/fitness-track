"""Zero to a validated environment, asserted rather than described (S01-T07).

The sprint's exit criterion is that a clean clone can follow the documentation
and end up with a working stack. This is the part of that claim a test can hold:
the services are healthy, the schema is at head, the LangGraph tables exist and
were not created by Alembic, and the bootstrap can run twice.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import asyncpg
import pytest
from scripts.bootstrap import LANGGRAPH_TABLES

from tests.conftest import ROOT, verified_dsn

# The four the application would actually reach through: the migrations tables
# are LangGraph's own bookkeeping. Kept separate from `LANGGRAPH_TABLES`, which
# is the full set bootstrap has to withdraw grants on.
GRAPH_STATE_TABLES = ("checkpoints", "checkpoint_blobs", "checkpoint_writes", "store")


def run_bootstrap(owner_dsn: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "scripts.bootstrap", *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        env={**os.environ, "MIGRATION_DATABASE_URL": verified_dsn(owner_dsn)},
    )


async def test_the_schema_is_at_a_single_head(owner: asyncpg.Connection) -> None:
    heads = await owner.fetch("SELECT version_num FROM alembic_version")
    assert len(heads) == 1


async def test_the_bootstrap_brings_a_fresh_database_up(
    disposable_database: str,
) -> None:
    """The clean-clone path, on a database that has never seen a migration."""
    result = run_bootstrap(disposable_database)
    assert result.returncode == 0, result.stderr
    assert "checkpointer and store ready" in result.stdout


async def test_the_bootstrap_is_idempotent(
    disposable_database: str, trusting_ssl_context: object
) -> None:
    """Run twice, because a bootstrap that can only run once is one nobody dares run."""
    assert run_bootstrap(disposable_database).returncode == 0
    second = run_bootstrap(disposable_database)
    assert second.returncode == 0, second.stderr

    connection = await asyncpg.connect(disposable_database, ssl=trusting_ssl_context)
    try:
        # Not duplicated, not destroyed.
        assert len(await connection.fetch("SELECT version_num FROM alembic_version")) == 1
        for table in GRAPH_STATE_TABLES:
            assert await connection.fetchval(
                "SELECT to_regclass($1) IS NOT NULL", f"public.{table}"
            ), table
    finally:
        await connection.close()


async def test_the_bootstrap_preserves_existing_data(
    disposable_database: str, trusting_ssl_context: object
) -> None:
    """Idempotent means "changes nothing", not "starts over"."""
    assert run_bootstrap(disposable_database).returncode == 0

    connection = await asyncpg.connect(disposable_database, ssl=trusting_ssl_context)
    try:
        tenant = await connection.fetchval(
            "INSERT INTO tenant (display_name) VALUES ('survivor') RETURNING id"
        )
        assert run_bootstrap(disposable_database).returncode == 0
        assert (
            await connection.fetchval("SELECT display_name FROM tenant WHERE id = $1", tenant)
            == "survivor"
        )
    finally:
        await connection.close()


def test_the_bootstrap_refuses_without_the_owner_credential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bootstrap runs as the schema owner, which the application never is.

    Checked against the process environment alone: with `.env` in play,
    unsetting the variable proves nothing, because the file still supplies it —
    which is correct in production and useless as a test.
    """
    from scripts.bootstrap import BootstrapError, _owner_dsn

    from tests.unit.test_startup import environment

    for name in list(os.environ):
        if name.startswith(("FITTRACK_", "DATABASE_", "MIGRATION_", "REDIS_", "QDRANT_")):
            monkeypatch.delenv(name, raising=False)
    for name, value in environment().items():
        monkeypatch.setenv(name, value)

    with pytest.raises(BootstrapError, match="MIGRATION_DATABASE_URL"):
        _owner_dsn(env_file=None)


async def test_alembic_did_not_create_the_langgraph_tables(
    owner: asyncpg.Connection,
) -> None:
    """They belong to the library, which upgrades them itself (spec 5.3).

    A migration owning them would fork their schema from the library's.
    """
    migration_source = Path(ROOT / "src/fittrack/db/migrations/versions").rglob("*.py")
    for path in migration_source:
        text = path.read_text(encoding="utf-8")
        for table in GRAPH_STATE_TABLES:
            assert f"CREATE TABLE {table}" not in text, f"{path.name} creates {table}"


async def test_every_service_of_the_topology_is_reachable(
    owner: asyncpg.Connection, redis_password: str, ca_file: Path
) -> None:
    """Postgres, Redis and Qdrant, over verified TLS — the three the app needs."""
    import httpx
    import redis.asyncio as redis

    from tests.conftest import HOST, _verifying_context

    assert await owner.fetchval("SELECT 1") == 1

    client = redis.Redis(
        host=HOST["redis"],
        port=6379,
        password=redis_password,
        ssl=True,
        ssl_cert_reqs="required",
        ssl_ca_certs=str(ca_file),
    )
    try:
        assert await client.ping()
    finally:
        await client.aclose()

    async with httpx.AsyncClient(verify=_verifying_context(ca_file)) as http:
        response = await http.get(f"https://{HOST['qdrant']}:6333/healthz")
    assert response.status_code == 200


# --------------------------------------------------------------------------- #
# Graph state is tenant data too
# --------------------------------------------------------------------------- #


async def test_the_application_cannot_touch_graph_state_after_bootstrap(
    disposable_database: str, trusting_ssl_context: object
) -> None:
    """The hole `ALTER DEFAULT PRIVILEGES` opens, closed and kept closed.

    Spec 19.1 grants the application DML on every table the owner creates from
    then on. Bootstrap creates LangGraph's tables as the owner, so the grant
    landed on them too — tables with no row-level security, holding one row per
    thread of every tenant's conversation. Nothing in the migration mentions
    them, and the "every table with a tenant column has a policy" test cannot
    see them: the tenant is inside `thread_id`, not in a column.
    """
    assert run_bootstrap(disposable_database).returncode == 0

    connection = await asyncpg.connect(disposable_database, ssl=trusting_ssl_context)
    try:
        for table in LANGGRAPH_TABLES:
            if not await connection.fetchval(
                "SELECT to_regclass($1) IS NOT NULL", f"public.{table}"
            ):
                continue
            for role in ("fittrack_app", "fittrack_runtime"):
                for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE"):
                    assert not await connection.fetchval(
                        "SELECT has_table_privilege($1, $2, $3)", role, table, privilege
                    ), f"{role} still has {privilege} on {table}"
    finally:
        await connection.close()


async def test_bootstrap_knows_every_table_langgraph_creates(
    disposable_database: str, trusting_ssl_context: object
) -> None:
    """`LANGGRAPH_TABLES` is a hand-written list, so it can go stale.

    A library upgrade that adds a table would add one the owner creates and the
    application inherits DML on, silently — the same bug again, one release
    later. This diffs the schema across `setup()` and fails if anything appeared
    that the revoke does not know about.
    """
    connection = await asyncpg.connect(disposable_database, ssl=trusting_ssl_context)
    try:
        before = {
            row["tablename"]
            for row in await connection.fetch(
                "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
            )
        }
        assert run_bootstrap(disposable_database).returncode == 0
        after = {
            row["tablename"]
            for row in await connection.fetch(
                "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
            )
        }
    finally:
        await connection.close()

    created = after - before
    # Everything Alembic makes is in the migration and already has a policy;
    # what is left is what `setup()` made.
    unknown = {
        table
        for table in created
        if table.startswith(("checkpoint", "store")) and table not in LANGGRAPH_TABLES
    }
    assert not unknown, f"LangGraph now creates tables bootstrap does not revoke on: {unknown}"
