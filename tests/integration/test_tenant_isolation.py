"""Proof that row level security actually isolates tenants.

The schema tests run as the container superuser, which bypasses RLS entirely.
These connect as fittrack_app -- NOSUPERUSER, NOBYPASSRLS -- which is how the
worker connects, so a policy that does not work fails here.

The seed is committed rather than left in an open transaction. An earlier
version rolled it back, which made `test_without_tenant_id_nothing_is_visible`
pass against an empty table: it would have passed with the policies deleted.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Coroutine
from typing import Any, NamedTuple

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

pytestmark = pytest.mark.integration

SetTenant = Callable[[int], Coroutine[Any, Any, AsyncConnection]]


class Seed(NamedTuple):
    alice: int
    bob: int


@pytest_asyncio.fixture
async def seed(migrated: str) -> AsyncIterator[Seed]:
    """Committed, so the application connection can see it; removed afterwards
    so tests stay independent."""
    engine = create_async_engine(migrated, isolation_level="AUTOCOMMIT")
    async with engine.connect() as owner:
        await owner.execute(text("INSERT INTO tenant (bsuid) VALUES ('alice'), ('bob')"))
        rows = await owner.execute(
            text("SELECT id FROM tenant WHERE bsuid IN ('alice','bob') ORDER BY bsuid")
        )
        alice, bob = (int(r[0]) for r in rows)
        for tenant_id, goal in ((alice, "hipertrofia"), (bob, "forca")):
            await owner.execute(
                text("INSERT INTO athlete_profile (tenant_id, goal) VALUES (:t, :g)"),
                {"t": tenant_id, "g": goal},
            )
        await owner.execute(
            text(
                "INSERT INTO exercise (slug, name, modality) "
                "VALUES ('supino_reto_barra', 'Supino reto', 'forca')"
            )
        )

        yield Seed(alice, bob)

        await owner.execute(text("DELETE FROM tenant WHERE bsuid IN ('alice','bob')"))
        await owner.execute(text("DELETE FROM exercise WHERE slug = 'supino_reto_barra'"))
    await engine.dispose()


async def test_app_role_is_not_superuser(conn: AsyncConnection) -> None:
    """If this fails every other isolation test is meaningless: a superuser
    bypasses RLS even with FORCE enabled."""
    row = await conn.execute(
        text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user")
    )
    is_super, bypasses = row.one()
    assert not is_super, "the application role must not be a superuser"
    assert not bypasses, "the application role must not have BYPASSRLS"


async def test_tenant_sees_only_its_own_rows(seed: Seed, as_tenant: SetTenant) -> None:
    conn = await as_tenant(seed.alice)

    rows = await conn.execute(text("SELECT tenant_id, goal FROM athlete_profile"))

    assert [(int(r[0]), r[1]) for r in rows] == [(seed.alice, "hipertrofia")]


async def test_tenant_cannot_write_as_another_tenant(seed: Seed, as_tenant: SetTenant) -> None:
    """WITH CHECK is what stops a forged tenant_id on insert. USING alone hides
    other rows while still letting one be written."""
    conn = await as_tenant(seed.alice)

    with pytest.raises(Exception, match=r"row-level security|policy"):
        await conn.execute(
            text(
                "INSERT INTO health_report (tenant_id, category, verbatim) "
                "VALUES (:t, 'dor', '\\x00'::bytea)"
            ),
            {"t": seed.bob},
        )


async def test_without_tenant_id_nothing_is_visible(seed: Seed, conn: AsyncConnection) -> None:
    """A repository that forgets SET LOCAL sees an empty table rather than
    everybody's rows: fail closed, not open.

    The seed guarantees there are rows to leak, so this cannot pass by accident.
    """
    rows = await conn.execute(text("SELECT count(*) FROM athlete_profile"))
    assert rows.scalar_one() == 0


async def test_global_catalog_rows_stay_readable(seed: Seed, as_tenant: SetTenant) -> None:
    """The resolver queries global and private rows together. Without the
    global-read policy the shared catalog vanishes the moment app.tenant_id is
    set, and every lookup falls through to creating a private exercise."""
    conn = await as_tenant(seed.alice)

    rows = await conn.execute(text("SELECT slug FROM exercise WHERE tenant_id IS NULL"))

    assert [r[0] for r in rows] == ["supino_reto_barra"]
