"""The migration cycle itself: upgrade, downgrade, upgrade, on a disposable database.

The roles matter as much as the tables. Connecting as the owner — or as anything
with BYPASSRLS — makes the whole of section 19.1 decorative: the policies exist
and are never evaluated, which is a silent failure rather than an error.
"""

from __future__ import annotations

import ssl

import asyncpg
import pytest


async def test_the_migration_reaches_a_single_head(owner: asyncpg.Connection) -> None:
    heads = await owner.fetch("SELECT version_num FROM alembic_version")
    assert len(heads) == 1, f"expected one head, found {[h['version_num'] for h in heads]}"


async def test_the_langgraph_tables_are_not_owned_by_alembic(
    owner: asyncpg.Connection,
) -> None:
    """Checkpointer and store tables are created by LangGraph's own bootstrap.

    Putting them in a migration would fork their schema from the library's, and
    the library upgrades them itself (spec 5.3).
    """
    for table in ("checkpoints", "checkpoint_blobs", "checkpoint_writes", "store"):
        assert not await owner.fetchval("SELECT to_regclass($1) IS NOT NULL", f"public.{table}"), (
            f"{table} should not be created by Alembic"
        )


# --------------------------------------------------------------------------- #
# Roles
# --------------------------------------------------------------------------- #


async def test_the_privilege_role_exists_and_cannot_log_in(
    owner: asyncpg.Connection,
) -> None:
    role = await owner.fetchrow(
        "SELECT rolcanlogin, rolsuper, rolbypassrls FROM pg_roles WHERE rolname = 'fittrack_app'"
    )
    assert role is not None, "fittrack_app is missing"
    assert not role["rolcanlogin"], "fittrack_app is a privilege role, not a principal"
    assert not role["rolsuper"]
    assert not role["rolbypassrls"]


async def test_the_runtime_principal_is_unprivileged(owner: asyncpg.Connection) -> None:
    role = await owner.fetchrow(
        """
        SELECT rolcanlogin, rolsuper, rolbypassrls
          FROM pg_roles WHERE rolname = 'fittrack_runtime'
        """
    )
    assert role is not None, "fittrack_runtime is missing"
    assert role["rolcanlogin"]
    assert not role["rolsuper"], "a superuser ignores RLS even with FORCE"
    assert not role["rolbypassrls"]


async def test_the_runtime_inherits_only_the_privilege_role(
    owner: asyncpg.Connection,
) -> None:
    memberships = await owner.fetch(
        """
        SELECT g.rolname
          FROM pg_auth_members m
          JOIN pg_roles r ON r.oid = m.member
          JOIN pg_roles g ON g.oid = m.roleid
         WHERE r.rolname = 'fittrack_runtime'
        """
    )
    assert {row["rolname"] for row in memberships} == {"fittrack_app"}


async def test_the_application_connects_as_the_runtime_principal(
    app: asyncpg.Connection,
) -> None:
    """The fixture the rest of the suite uses must not be the owner."""
    assert await app.fetchval("SELECT current_user") == "fittrack_runtime"
    assert not await app.fetchval("SELECT usesuper FROM pg_user WHERE usename = current_user")


async def test_the_application_cannot_create_a_table(app: asyncpg.Connection) -> None:
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        await app.execute("CREATE TABLE should_not_exist (id int)")


async def test_the_application_does_not_own_the_domain_tables(
    owner: asyncpg.Connection,
) -> None:
    """Owners bypass RLS unless FORCE is set; not owning is the sturdier guarantee."""
    owned = await owner.fetch(
        """
        SELECT tablename FROM pg_tables
         WHERE schemaname = 'public' AND tableowner IN ('fittrack_app', 'fittrack_runtime')
        """
    )
    assert not owned, f"tables owned by an application role: {[r['tablename'] for r in owned]}"


async def test_the_application_can_read_and_write_the_domain(
    app: asyncpg.Connection,
) -> None:
    grants = await app.fetch(
        """
        SELECT privilege_type FROM information_schema.role_table_grants
         WHERE table_schema = 'public' AND table_name = 'workout_session'
           AND grantee IN ('fittrack_app', 'fittrack_runtime')
        """
    )
    assert {"SELECT", "INSERT", "UPDATE", "DELETE"} <= {r["privilege_type"] for r in grants}


# --------------------------------------------------------------------------- #
# The cycle, on a disposable database
# --------------------------------------------------------------------------- #


async def test_downgrade_then_upgrade_rebuilds_the_schema(
    disposable_database: str, trusting_ssl_context: ssl.SSLContext
) -> None:
    """Proves the downgrade is real, not a stub that would strand a rollback.

    On a database created for this test and dropped after it. Pointing it at the
    application database would have wiped every local workout the first time a
    developer ran `make test-in-worker` — `downgrade base` drops every table of
    section 5.2, and the previous version of this test did exactly that.
    """
    import os
    import subprocess
    import sys
    from pathlib import Path

    from tests.conftest import verified_dsn

    root = Path(__file__).resolve().parents[2]
    env = {**os.environ, "MIGRATION_DATABASE_URL": verified_dsn(disposable_database)}

    def alembic(*args: str) -> None:
        result = subprocess.run(
            [sys.executable, "-m", "alembic", *args],
            cwd=root,
            capture_output=True,
            text=True,
            env=env,
        )
        assert result.returncode == 0, f"alembic {' '.join(args)}:\n{result.stderr}"

    alembic("upgrade", "head")
    connection = await asyncpg.connect(disposable_database, ssl=trusting_ssl_context)
    try:
        assert await connection.fetchval("SELECT to_regclass('public.tenant') IS NOT NULL")

        alembic("downgrade", "base")
        assert not await connection.fetchval("SELECT to_regclass('public.tenant') IS NOT NULL")

        alembic("upgrade", "head")
        assert await connection.fetchval("SELECT to_regclass('public.tenant') IS NOT NULL")
        heads = await connection.fetch("SELECT version_num FROM alembic_version")
        assert len(heads) == 1
    finally:
        await connection.close()


async def test_the_migration_refuses_a_missing_runtime_principal(
    disposable_database: str, trusting_ssl_context: ssl.SSLContext
) -> None:
    """A migration must not create a LOGIN role, because it must not set a password.

    On an upgrade against an existing volume initdb is skipped, so a role created
    here would never get one, and every service would fail to connect for a
    reason that looks like a network fault. The migration refuses instead.

    Exercised by running the guard's own SQL against a role name that does not
    exist. Renaming the real principal would work too, and would break every
    other test in the cluster if this one failed halfway.
    """
    from fittrack.db.migrations.versions._0001_initial_schema import RUNTIME_REQUIRED

    connection = await asyncpg.connect(disposable_database, ssl=trusting_ssl_context)
    try:
        with pytest.raises(asyncpg.RaiseError, match="does not exist"):
            await connection.execute(
                RUNTIME_REQUIRED.replace("fittrack_runtime", "fittrack_absent_role")
            )
        # And it passes for a principal that does exist, so the test proves the
        # guard rather than a typo.
        await connection.execute(RUNTIME_REQUIRED)
    finally:
        await connection.close()


async def test_the_runtime_principal_cannot_bypass_rls(owner: asyncpg.Connection) -> None:
    """The second guard: whatever created the role, it must not ignore policies."""
    flags = await owner.fetchrow(
        "SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = 'fittrack_runtime'"
    )
    assert flags is not None
    assert not flags["rolsuper"]
    assert not flags["rolbypassrls"]
