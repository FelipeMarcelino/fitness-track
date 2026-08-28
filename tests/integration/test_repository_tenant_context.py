"""The first barrier: a repository that cannot run without a tenant (spec 19.1).

RLS is the second, and it exists because this one depends on people
remembering. So this one is built so there is nothing to remember — the
`tenant_id` arrives in the constructor and no method takes it — and these tests
are mostly about what the type refuses to do.

The interesting case is the one in the middle: a repository whose transaction
was never bound. RLS alone would return zero rows, which reads like "no data"
rather than "wrong context" — a bug wearing the costume of an absence. The
repository raises instead.
"""

from __future__ import annotations

import secrets
from collections.abc import AsyncIterator

import asyncpg
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from fittrack.db.engine import split_ssl_arguments
from fittrack.repositories.base import (
    TenantContextError,
    TenantRepository,
    current_tenant,
    set_tenant,
    tenant_transaction,
)
from tests.conftest import CA_FILE


@pytest.fixture
async def session(app_dsn: str, migrated: None) -> AsyncIterator[AsyncSession]:
    url, ssl_args = split_ssl_arguments(
        app_dsn.replace("postgresql://", "postgresql+asyncpg://")
        + f"?sslmode=verify-full&sslrootcert={CA_FILE}"
    )
    engine = create_async_engine(url, connect_args=ssl_args)
    async with async_sessionmaker(engine, expire_on_commit=False)() as opened:
        yield opened
    await engine.dispose()


async def make_tenant(owner: asyncpg.Connection) -> int:
    tenant_id: int = await owner.fetchval(
        "INSERT INTO tenant (display_name) VALUES ('repo') RETURNING id"
    )
    await owner.execute(
        "INSERT INTO workout_session (tenant_id, local_date) VALUES ($1, CURRENT_DATE)",
        tenant_id,
    )
    return tenant_id


# --------------------------------------------------------------------------- #
# Construction
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("tenant_id", [0, -1])
async def test_a_repository_cannot_be_built_without_a_real_tenant(
    session: AsyncSession, tenant_id: int
) -> None:
    with pytest.raises(TenantContextError):
        TenantRepository(session, tenant_id)


async def test_the_tenant_is_held_on_the_instance(
    session: AsyncSession, owner: asyncpg.Connection
) -> None:
    """No method takes it, so no call site can omit it."""
    tenant = await make_tenant(owner)
    assert TenantRepository(session, tenant).tenant_id == tenant


# --------------------------------------------------------------------------- #
# The context
# --------------------------------------------------------------------------- #


async def test_a_bound_transaction_reads_its_own_rows(
    session: AsyncSession, owner: asyncpg.Connection
) -> None:
    tenant = await make_tenant(owner)
    async with tenant_transaction(session, tenant):
        rows = await TenantRepository(session, tenant).fetch_all("SELECT id FROM workout_session")
    assert len(rows) >= 1


async def test_a_bound_transaction_sees_no_other_tenant(
    session: AsyncSession, owner: asyncpg.Connection
) -> None:
    """The statement carries no tenant predicate — the policy supplies it.

    A repository that added one would be duplicating the policy, and the two
    would eventually disagree about which is authoritative.
    """
    mine = await make_tenant(owner)
    theirs = await make_tenant(owner)

    async with tenant_transaction(session, mine):
        rows = await TenantRepository(session, mine).fetch_all(
            "SELECT id FROM workout_session WHERE tenant_id = :other", other=theirs
        )
    assert rows == []


async def test_an_unbound_transaction_raises_rather_than_reading_nothing(
    session: AsyncSession, owner: asyncpg.Connection
) -> None:
    """Zero rows would read as an absence of data rather than of context."""
    tenant = await make_tenant(owner)
    repository = TenantRepository(session, tenant)
    async with session.begin():
        with pytest.raises(TenantContextError, match=r"app\.tenant_id"):
            await repository.fetch_all("SELECT id FROM workout_session")


async def test_a_repository_refuses_another_tenants_context(
    session: AsyncSession, owner: asyncpg.Connection
) -> None:
    """A mismatch is a bug in the caller, and silence would hide it."""
    mine = await make_tenant(owner)
    theirs = await make_tenant(owner)

    async with tenant_transaction(session, theirs):
        with pytest.raises(TenantContextError, match="bound to tenant"):
            await TenantRepository(session, mine).fetch_all("SELECT id FROM workout_session")


async def test_the_setting_does_not_survive_the_transaction(
    session: AsyncSession, owner: asyncpg.Connection
) -> None:
    """`SET LOCAL` is what makes a pooled connection safe.

    Without it, one tenant's context would outlive its transaction and be
    inherited by whoever picked the connection up next.
    """
    tenant = await make_tenant(owner)
    async with tenant_transaction(session, tenant):
        assert await current_tenant(session) == tenant

    async with session.begin():
        assert await current_tenant(session) is None


async def test_a_write_lands_under_the_bound_tenant(
    session: AsyncSession, owner: asyncpg.Connection
) -> None:
    tenant = await make_tenant(owner)
    label = f"sessao-{secrets.token_hex(6)}"

    async with tenant_transaction(session, tenant):
        # Closed: `ux_session_one_open` allows a tenant only one open session,
        # and the seed already holds it.
        await TenantRepository(session, tenant).execute(
            "INSERT INTO workout_session (tenant_id, local_date, label, status)"
            " VALUES (:tenant, CURRENT_DATE, :label, 'closed_explicit')",
            tenant=tenant,
            label=label,
        )

    assert (
        await owner.fetchval("SELECT tenant_id FROM workout_session WHERE label = $1", label)
        == tenant
    )


async def test_a_write_for_another_tenant_is_refused_by_the_policy(
    session: AsyncSession, owner: asyncpg.Connection
) -> None:
    """The `WITH CHECK` half, reached through the repository."""
    mine = await make_tenant(owner)
    theirs = await make_tenant(owner)

    with pytest.raises(Exception, match=r"row-level security|violates"):
        async with tenant_transaction(session, mine):
            await TenantRepository(session, mine).execute(
                "INSERT INTO workout_session (tenant_id, local_date) VALUES (:other, CURRENT_DATE)",
                other=theirs,
            )


async def test_the_tenant_value_reaches_sql_as_a_parameter(
    session: AsyncSession, owner: asyncpg.Connection
) -> None:
    """Never interpolated: it would be the one place a value becomes syntax."""
    tenant = await make_tenant(owner)
    async with session.begin():
        await set_tenant(session, tenant)
        assert await current_tenant(session) == tenant


@pytest.mark.parametrize("tenant_id", [0, -1])
async def test_setting_a_nonsense_tenant_is_refused(session: AsyncSession, tenant_id: int) -> None:
    async with session.begin():
        with pytest.raises(TenantContextError):
            await set_tenant(session, tenant_id)


# --------------------------------------------------------------------------- #
# What a write reports back
# --------------------------------------------------------------------------- #


async def test_execute_reports_how_many_rows_it_touched(
    session: AsyncSession, owner: asyncpg.Connection
) -> None:
    """The count is the caller's only signal that a write did anything."""
    tenant_id = await make_tenant(owner)

    async with tenant_transaction(session, tenant_id):
        repository = TenantRepository(session, tenant_id)
        touched = await repository.execute(
            "UPDATE workout_session SET label = :label WHERE tenant_id = :tenant",
            label="leg day",
            tenant=tenant_id,
        )

    assert touched == 1


async def test_a_write_filtered_by_the_policy_reports_zero(
    session: AsyncSession, owner: asyncpg.Connection
) -> None:
    """RLS removes rows from an UPDATE silently, so the statement succeeds.

    Returning `None` from `execute` threw away the only evidence that the write
    had reached nothing — a cross-tenant update would look exactly like a
    successful one.
    """
    mine = await make_tenant(owner)
    theirs = await make_tenant(owner)

    async with tenant_transaction(session, mine):
        repository = TenantRepository(session, mine)
        touched = await repository.execute(
            "UPDATE workout_session SET label = 'hijacked' WHERE tenant_id = :tenant",
            tenant=theirs,
        )

    assert touched == 0
    assert (
        await owner.fetchval(
            "SELECT count(*) FROM workout_session WHERE tenant_id = $1 AND label = 'hijacked'",
            theirs,
        )
        == 0
    )


# --------------------------------------------------------------------------- #
# Who owns the transaction
# --------------------------------------------------------------------------- #


async def test_tenant_transaction_refuses_a_session_already_in_a_transaction(
    session: AsyncSession, owner: asyncpg.Connection
) -> None:
    """`SET LOCAL` dies with the transaction, so this has to own one.

    SQLAlchemy autobegins on the first statement. Joining that transaction
    would leave the binding alive after the `async with` closed, and would
    quietly rebind a transaction someone else had already bound.
    """
    tenant_id = await make_tenant(owner)
    await session.execute(text("SELECT 1"))  # autobegins
    assert session.in_transaction()

    with pytest.raises(TenantContextError, match="already has an open transaction"):
        async with tenant_transaction(session, tenant_id):
            pass  # pragma: no cover - the context must not open

    await session.rollback()


async def test_the_binding_does_not_outlive_its_transaction(
    session: AsyncSession, owner: asyncpg.Connection
) -> None:
    """The `LOCAL` in `SET LOCAL`, stated as a test.

    This is what makes the context safe under a pooled connection: the next
    tenant to borrow it cannot inherit the last one's binding.
    """
    tenant_id = await make_tenant(owner)

    async with tenant_transaction(session, tenant_id):
        assert await current_tenant(session) == tenant_id

    assert await current_tenant(session) is None
    await session.rollback()


async def test_an_unbound_repository_does_not_strand_a_transaction(
    session: AsyncSession, owner: asyncpg.Connection
) -> None:
    """Refusing to run must not leave a connection idle in transaction.

    `current_tenant` issues a query, and a query on an unbound session
    autobegins the transaction that `_assert_bound` then refuses to use. The
    caller sees only the exception, so nothing ever closes it and the
    connection sits held until the pool recycles it.
    """
    tenant_id = await make_tenant(owner)
    repository = TenantRepository(session, tenant_id)

    with pytest.raises(TenantContextError, match="no transaction is open"):
        await repository.fetch_all("SELECT 1")

    assert not session.in_transaction()
