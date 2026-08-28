"""Zero to a validated environment, asserted rather than described (S01-T07).

The sprint's exit criterion is that a clean clone can follow the documentation
and end up with a working stack. This is the part of that claim a test can hold:
the services are healthy, the schema is at head, the LangGraph tables exist and
were not created by Alembic, and the bootstrap can run twice.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import asyncpg
import pytest
from scripts.bootstrap import LANGGRAPH_TABLES, BootstrapError

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
    # `CREATE TABLE <name>` alone matches neither spelling a migration would
    # realistically use: LangGraph itself writes `CREATE TABLE IF NOT EXISTS`,
    # which is what a copy-paste would carry, and Alembic's own idiom is
    # `op.create_table("checkpoints", ...)`. The test could not fail.
    patterns = [
        "CREATE TABLE {table}",
        "CREATE TABLE IF NOT EXISTS {table}",
        'op.create_table("{table}"',
        "op.create_table('{table}'",
    ]
    for path in Path(ROOT / "src/fittrack/db/migrations/versions").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        collapsed = " ".join(text.split())
        for table in LANGGRAPH_TABLES:
            for pattern in patterns:
                needle = pattern.format(table=table)
                assert needle not in collapsed, f"{path.name} creates {table}: {needle}"


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

    # Everything the migrations create, taken from the migrations themselves
    # rather than guessed at — so what is left over really is what `setup()`
    # made. Filtering on a `checkpoint`/`store` prefix instead was narrower than
    # the docstring claimed: this version of langgraph-checkpoint-postgres
    # already creates `vector_migrations` when an index config is passed, and a
    # prefix filter would wave it through.
    from_migrations = tables_named_in_migrations()
    unknown = (after - before) - set(LANGGRAPH_TABLES) - from_migrations - {"alembic_version"}
    assert not unknown, f"bootstrap created tables the revoke does not know about: {unknown}"


def tables_named_in_migrations() -> set[str]:
    """Every table name a migration creates, read out of the migration source."""
    names: set[str] = set()
    pattern = re.compile(
        r"""CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?"?(\w+)"?"""
        r"""|op\.create_table\(\s*["'](\w+)["']""",
        re.IGNORECASE,
    )
    for path in Path(ROOT / "src/fittrack/db/migrations/versions").rglob("*.py"):
        collapsed = " ".join(path.read_text(encoding="utf-8").split())
        for match in pattern.finditer(collapsed):
            names.add(match.group(1) or match.group(2))
    return names


async def test_check_does_not_call_an_unmigrated_database_ready(
    disposable_database: str,
) -> None:
    """`--check` answers "is this environment ready?", so it must not lie.

    `alembic current` exits 0 and prints nothing when there is no
    `alembic_version` table at all, and reading that as "ok" reported a
    database with no schema on it as healthy.
    """
    result = run_bootstrap(disposable_database, "--check")

    assert result.returncode == 1, result.stdout
    assert "no migrations have been applied" in result.stderr

    assert run_bootstrap(disposable_database).returncode == 0
    migrated = run_bootstrap(disposable_database, "--check")
    assert migrated.returncode == 0
    assert "0003" in migrated.stdout


async def test_the_revoke_refuses_to_report_success_after_finding_nothing(
    disposable_migrated_database: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Its only failure mode must not be silence.

    `setup()` runs moments before, so finding no tables does not mean there was
    nothing to revoke — it means the lookup went somewhere else, and the
    application kept full DML on every tenant's graph state. The old version
    would have printed "revoked on 0 table(s)" and exited 0.
    """
    from scripts.bootstrap import revoke_application_grants

    monkeypatch.setattr("scripts.bootstrap.LANGGRAPH_TABLES", ("no_such_table",))

    with pytest.raises(BootstrapError, match="has NOT been revoked"):
        await revoke_application_grants(
            verified_dsn(disposable_migrated_database).replace("+asyncpg", "")
        )
