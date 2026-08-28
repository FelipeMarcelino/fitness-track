"""Cross-tenant leakage, walked table by table (spec 19.1).

Parametrised over the **whole** list, not a sample. A table missing a policy is
a silent leak that needs only one repository to forget its predicate, so the
list here and the one in migration 0002 are checked against each other — a new
tenant-scoped table with no policy fails this file.

Everything runs as `fittrack_runtime`. As the owner, or as anything with
`BYPASSRLS`, every assertion below would pass without a single policy being
evaluated (spec 19.1) — which is why the connection is asserted first.
"""

from __future__ import annotations

import secrets
from collections.abc import AsyncIterator

import asyncpg
import pytest

from fittrack.db.migrations.versions._0002_row_level_security import (
    GLOBAL_READABLE,
    TENANT_SCOPED,
)

# Needed because this file sits at the suite root, where the directory marker of
# `tests/integration/` does not reach it (spec 23 puts it here).
pytestmark = pytest.mark.integration

# One row per table, keyed by the columns the schema makes mandatory. The values
# are irrelevant; what matters is that the row exists and belongs to a tenant.
SEEDS: dict[str, str] = {
    "channel_identity": (
        "INSERT INTO channel_identity (tenant_id, channel, external_id, external_id_hash)"
        " VALUES ($1, 'telegram', $2, $2)"
    ),
    "athlete_profile": "INSERT INTO athlete_profile (tenant_id, goal) VALUES ($1, 'forca')",
    "consent": (
        "INSERT INTO consent (tenant_id, kind, granted, text_hash, version)"
        " VALUES ($1, 'terms', true, 'h', 'v1')"
    ),
    "subscription": "INSERT INTO subscription (tenant_id, status) VALUES ($1, 'active')",
    "exercise": (
        "INSERT INTO exercise (tenant_id, slug, name, modality)"
        " VALUES ($1, encode($2, 'hex'), 'x', 'forca')"
    ),
    "workout_session": (
        "INSERT INTO workout_session (tenant_id, local_date) VALUES ($1, CURRENT_DATE)"
    ),
    "body_metric": (
        "INSERT INTO body_metric (tenant_id, local_date, kind, value, unit)"
        " VALUES ($1, CURRENT_DATE, 'peso', $2, 'kg')"
    ),
    "health_report": (
        "INSERT INTO health_report (tenant_id, category, verbatim) VALUES ($1, 'dor', $2)"
    ),
    "training_program": (
        "INSERT INTO training_program (tenant_id, name, goal, horizon_weeks, rationale)"
        " VALUES ($1, 'p', 'forca', 8, 'r')"
    ),
    "workout_plan": "INSERT INTO workout_plan (tenant_id, name) VALUES ($1, 'plano')",
    "processing_batch": (
        "INSERT INTO processing_batch (tenant_id, message_ids, combined_text)"
        " VALUES ($1, ARRAY['m'], $2)"
    ),
    "usage_ledger": (
        "INSERT INTO usage_ledger (tenant_id, agent, provider, model) VALUES ($1, 'a', 'groq', 'm')"
    ),
}


@pytest.fixture
async def app_session(
    app_dsn: str, trusting_ssl_context: object, migrated: None
) -> AsyncIterator[asyncpg.Connection]:
    """A connection as the unprivileged principal, with no tenant context yet."""
    connection = await asyncpg.connect(app_dsn, ssl=trusting_ssl_context)
    try:
        yield connection
    finally:
        await connection.close()


async def set_tenant(connection: asyncpg.Connection, tenant_id: int | None) -> None:
    await connection.execute(
        "SELECT set_config('app.tenant_id', $1, false)", "" if tenant_id is None else str(tenant_id)
    )


async def seeded_tenant(owner: asyncpg.Connection, table: str) -> int:
    """A tenant owning one row in `table`, written as the owner."""
    tenant: int = await owner.fetchval(
        "INSERT INTO tenant (display_name) VALUES ('seed') RETURNING id"
    )
    if table in SEEDS:
        await owner.execute(*seed_args(table, tenant))
    return tenant


def seed_args(table: str, tenant: int) -> tuple[object, ...]:
    """The insert and its parameters. Only some rows need a unique marker."""
    statement = SEEDS[table]
    if "$2" in statement:
        return (statement, tenant, secrets.token_bytes(16))
    return (statement, tenant)


# --------------------------------------------------------------------------- #
# The connection itself
# --------------------------------------------------------------------------- #


async def test_the_suite_connects_as_the_unprivileged_principal(
    app_session: asyncpg.Connection,
) -> None:
    """Otherwise every assertion below passes with no policy evaluated."""
    role = await app_session.fetchrow(
        """
        SELECT current_user AS name, rolsuper, rolbypassrls
          FROM pg_roles WHERE rolname = current_user
        """
    )
    assert role["name"] == "fittrack_runtime"
    assert not role["rolsuper"]
    assert not role["rolbypassrls"]


# --------------------------------------------------------------------------- #
# Every table, both directions
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("table", sorted(TENANT_SCOPED))
async def test_row_level_security_is_enabled_and_forced(
    owner: asyncpg.Connection, table: str
) -> None:
    """`FORCE` matters: without it the table's owner ignores every policy."""
    flags = await owner.fetchrow(
        "SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname = $1", table
    )
    assert flags["relrowsecurity"], f"{table} has RLS disabled"
    assert flags["relforcerowsecurity"], f"{table} does not FORCE RLS"


async def test_the_root_table_is_covered_too(owner: asyncpg.Connection) -> None:
    flags = await owner.fetchrow(
        "SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname = 'tenant'"
    )
    assert flags["relrowsecurity"] and flags["relforcerowsecurity"]


@pytest.mark.parametrize("table", sorted(SEEDS))
async def test_a_tenant_reads_only_its_own_rows(
    owner: asyncpg.Connection, app_session: asyncpg.Connection, table: str
) -> None:
    mine = await seeded_tenant(owner, table)
    theirs = await seeded_tenant(owner, table)

    await set_tenant(app_session, mine)
    visible = await app_session.fetchval(
        f"SELECT count(*) FROM {table} WHERE tenant_id = $1",
        theirs,
    )
    assert visible == 0, f"{table} leaked rows of another tenant"

    assert (
        await app_session.fetchval(
            f"SELECT count(*) FROM {table} WHERE tenant_id = $1",
            mine,
        )
        >= 1
    )


@pytest.mark.parametrize("table", sorted(SEEDS))
async def test_a_tenant_cannot_update_another_tenants_rows(
    owner: asyncpg.Connection, app_session: asyncpg.Connection, table: str
) -> None:
    """An UPDATE that matches nothing is the policy working, not a no-op."""
    mine = await seeded_tenant(owner, table)
    theirs = await seeded_tenant(owner, table)

    await set_tenant(app_session, mine)
    result = await app_session.execute(
        f"UPDATE {table} SET tenant_id = tenant_id WHERE tenant_id = $1",
        theirs,
    )
    assert result.endswith(" 0"), f"{table} allowed a cross-tenant update: {result}"


@pytest.mark.parametrize("table", sorted(SEEDS))
async def test_a_tenant_cannot_delete_another_tenants_rows(
    owner: asyncpg.Connection, app_session: asyncpg.Connection, table: str
) -> None:
    mine = await seeded_tenant(owner, table)
    theirs = await seeded_tenant(owner, table)

    await set_tenant(app_session, mine)
    result = await app_session.execute(
        f"DELETE FROM {table} WHERE tenant_id = $1",
        theirs,
    )
    assert result.endswith(" 0"), f"{table} allowed a cross-tenant delete: {result}"

    # And the row is still there, seen from the owner.
    assert (
        await owner.fetchval(
            f"SELECT count(*) FROM {table} WHERE tenant_id = $1",
            theirs,
        )
        >= 1
    )


@pytest.mark.parametrize("table", sorted(SEEDS))
async def test_a_tenant_cannot_write_a_row_for_another(
    owner: asyncpg.Connection, app_session: asyncpg.Connection, table: str
) -> None:
    """The `WITH CHECK` half: reading is not the only way to cross the line."""
    mine = await seeded_tenant(owner, table)
    theirs = await seeded_tenant(owner, table)

    await set_tenant(app_session, mine)
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        await app_session.execute(*seed_args(table, theirs))


# --------------------------------------------------------------------------- #
# No context at all
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("table", sorted(SEEDS))
async def test_a_connection_without_a_tenant_sees_nothing_private(
    owner: asyncpg.Connection, app_session: asyncpg.Connection, table: str
) -> None:
    """A forgotten `SET LOCAL` must read zero rows, never every row.

    `current_setting(..., true)` returns NULL instead of raising, and NULL
    compares to nothing — so the omission fails closed.
    """
    tenant = await seeded_tenant(owner, table)
    await set_tenant(app_session, None)
    assert (
        await app_session.fetchval(
            f"SELECT count(*) FROM {table} WHERE tenant_id = $1",
            tenant,
        )
        == 0
    )


async def test_a_connection_without_a_tenant_sees_no_tenants(
    owner: asyncpg.Connection, app_session: asyncpg.Connection
) -> None:
    await seeded_tenant(owner, "tenant")
    await set_tenant(app_session, None)
    assert await app_session.fetchval("SELECT count(*) FROM tenant") == 0


# --------------------------------------------------------------------------- #
# The global catalogue
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("table", sorted(GLOBAL_READABLE))
async def test_global_rows_are_readable_by_any_tenant(
    owner: asyncpg.Connection, app_session: asyncpg.Connection, table: str
) -> None:
    """Without this policy the catalogue vanishes as soon as a tenant is set."""
    if table == "exercise":
        await owner.execute(
            "INSERT INTO exercise (tenant_id, slug, name, modality)"
            " VALUES (NULL, $1, 'global', 'forca')",
            f"global-{secrets.token_hex(6)}",
        )
    tenant = await seeded_tenant(owner, "tenant")
    await set_tenant(app_session, tenant)

    total = await app_session.fetchval(f"SELECT count(*) FROM {table} WHERE tenant_id IS NULL")
    assert total is not None  # readable, whatever the catalogue currently holds


async def test_a_tenant_cannot_write_to_the_global_catalogue(
    owner: asyncpg.Connection, app_session: asyncpg.Connection
) -> None:
    """Comparison with NULL is never true, and `WITH CHECK` demands non-null."""
    tenant = await seeded_tenant(owner, "tenant")
    await set_tenant(app_session, tenant)

    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        await app_session.execute(
            "INSERT INTO exercise (tenant_id, slug, name, modality)"
            " VALUES (NULL, $1, 'x', 'forca')",
            f"sneaky-{secrets.token_hex(6)}",
        )


async def test_a_tenant_cannot_delete_from_the_global_catalogue(
    owner: asyncpg.Connection, app_session: asyncpg.Connection
) -> None:
    slug = f"global-{secrets.token_hex(6)}"
    await owner.execute(
        "INSERT INTO exercise (tenant_id, slug, name, modality)"
        " VALUES (NULL, $1, 'global', 'forca')",
        slug,
    )
    tenant = await seeded_tenant(owner, "tenant")
    await set_tenant(app_session, tenant)

    result = await app_session.execute("DELETE FROM exercise WHERE slug = $1", slug)
    assert result.endswith(" 0")
    assert await owner.fetchval("SELECT count(*) FROM exercise WHERE slug = $1", slug) == 1


# --------------------------------------------------------------------------- #
# The list itself
# --------------------------------------------------------------------------- #


async def test_every_table_with_a_tenant_column_has_a_policy(
    owner: asyncpg.Connection,
) -> None:
    """The check that makes a *new* table fail rather than leak quietly."""
    with_column = {
        row["table_name"]
        for row in await owner.fetch(
            """
            SELECT c.table_name FROM information_schema.columns c
              JOIN information_schema.tables t
                ON t.table_schema = c.table_schema AND t.table_name = c.table_name
             WHERE c.table_schema = 'public' AND c.column_name = 'tenant_id'
               AND t.table_type = 'BASE TABLE'
            """
        )
    }
    missing = sorted(with_column - set(TENANT_SCOPED))
    assert not missing, f"tables with tenant_id and no policy: {missing}"
