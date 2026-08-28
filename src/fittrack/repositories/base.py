"""The tenant-scoped repository contract (spec 19.1).

The first barrier of the two. RLS is the second, and it exists because this one
depends on people remembering — so this one is built so there is nothing to
remember: a repository takes its `tenant_id` in the constructor and offers no
method that could run without it.

`SET LOCAL app.tenant_id` is what makes the policies do anything, and `LOCAL`
is what makes it safe under a connection pool: the setting dies with the
transaction, so one tenant's context cannot survive into another's query on a
recycled connection.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from typing import Any, cast

from sqlalchemy import CursorResult, text
from sqlalchemy.ext.asyncio import AsyncSession


class TenantContextError(RuntimeError):
    """A query was attempted without a tenant context, or with the wrong one."""


TENANT_SETTING = "app.tenant_id"


async def set_tenant(session: AsyncSession, tenant_id: int) -> None:
    """Bind the transaction to one tenant.

    `set_config(..., true)` is the `SET LOCAL` form. Parameterised rather than
    interpolated: the value reaches SQL as a bind parameter, so it cannot be
    anything but a value (spec 22, "SQL injection").
    """
    if tenant_id <= 0:
        raise TenantContextError("tenant_id must be a positive integer")
    await session.execute(
        text(f"SELECT set_config('{TENANT_SETTING}', :tenant_id, true)"),
        {"tenant_id": str(tenant_id)},
    )


async def current_tenant(session: AsyncSession) -> int | None:
    """Whichever tenant this transaction is bound to, if any."""
    raw = await session.scalar(
        text(f"SELECT NULLIF(current_setting('{TENANT_SETTING}', true), '')")
    )
    return int(raw) if raw else None


@asynccontextmanager
async def tenant_transaction(session: AsyncSession, tenant_id: int) -> AsyncIterator[AsyncSession]:
    """A transaction that is bound to a tenant before it does anything else.

    Refuses a session that is already in a transaction. `SET LOCAL` lives and
    dies with the transaction, so this has to own one: joining someone else's
    would leave the binding in place after the `async with` closed, and would
    silently overwrite a tenant that transaction had already been bound to.
    SQLAlchemy autobegins on the first statement, so an already-open
    transaction means a query ran before this call — commit or roll it back.
    """
    if session.in_transaction():
        raise TenantContextError(
            "session already has an open transaction: tenant_transaction() must open its own, "
            "so that SET LOCAL app.tenant_id is discarded with it"
        )
    async with session.begin():
        await set_tenant(session, tenant_id)
        yield session


class TenantRepository:
    """Base for every repository that touches tenant-scoped data.

    Holding the `tenant_id` on the instance is the point: there is no method
    that takes it as an argument, so there is no call site that can omit it.
    """

    def __init__(self, session: AsyncSession, tenant_id: int) -> None:
        if tenant_id <= 0:
            raise TenantContextError("a repository must be constructed with a tenant")
        self._session = session
        self._tenant_id = tenant_id

    @property
    def tenant_id(self) -> int:
        return self._tenant_id

    async def bind(self) -> None:
        """Apply this repository's tenant to the current transaction."""
        await set_tenant(self._session, self._tenant_id)

    async def _assert_bound(self) -> None:
        """Refuse to run against another tenant's context, or none at all.

        Without this the policies would simply return nothing, and an empty
        result reads like "no data" rather than "wrong context" — a bug that
        looks like an absence.
        """
        bound = await current_tenant(self._session)
        if bound is None:
            raise TenantContextError(
                f"no {TENANT_SETTING} is set: open the transaction with tenant_transaction()"
            )
        if bound != self._tenant_id:
            raise TenantContextError(
                f"transaction is bound to tenant {bound}, repository to {self._tenant_id}"
            )

    async def fetch_all(self, statement: str, **params: Any) -> Sequence[Any]:
        """Run a read in this repository's tenant context.

        The statement carries no tenant predicate of its own — RLS supplies it.
        A repository that added one would be duplicating the policy, and the
        two would eventually disagree.
        """
        await self._assert_bound()
        result = await self._session.execute(text(statement), params)
        return result.fetchall()

    async def fetch_one(self, statement: str, **params: Any) -> Any:
        await self._assert_bound()
        result = await self._session.execute(text(statement), params)
        return result.fetchone()

    async def execute(self, statement: str, **params: Any) -> int:
        """Run a write in this repository's tenant context, returning rowcount.

        The count is the caller's only way to tell "nothing matched" from "the
        policy filtered it out": RLS removes rows from an UPDATE or DELETE
        silently, so an unchecked write against another tenant's row succeeds
        and does nothing. Discarding the number here would discard the only
        evidence.
        """
        await self._assert_bound()
        # `rowcount` lives on `CursorResult`, which is what `execute` actually
        # returns for DML; the declared `Result` is the common supertype.
        result = cast(CursorResult[Any], await self._session.execute(text(statement), params))
        return result.rowcount
