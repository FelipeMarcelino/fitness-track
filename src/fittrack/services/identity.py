"""Resolving an account to a tenant — the one operation that precedes tenancy.

Every other query in the system runs inside `SET LOCAL app.tenant_id`. This one
cannot: at the moment a webhook arrives the only facts are a channel and an
opaque identifier, and finding the tenant *is* the operation. There is nothing
to set the setting to yet.

So it goes through narrowly scoped `SECURITY DEFINER` functions instead
(migrations 0002 and 0004), owned by a role that can do nothing else and
callable only by `fittrack_app`. That is the entire pre-tenant surface: the
application cannot *write* to `channel_identity` directly, so creating or
revoking an account has to go through a named boundary operation.

It can read its own rows — RLS scopes that to the bound tenant, which is the
same protection every other table gets, and a `deliver` that has to address a
message will need it. What it cannot do is find a tenant it is not already
bound to, which is the whole reason the resolve function is `SECURITY DEFINER`.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from fittrack.security.crypto import ColumnCipher, identity_aad
from fittrack.security.identity_hash import identity_hash

# The only SQLSTATE that means "someone else created this identity first".
UNIQUE_VIOLATION = "23505"


@dataclass(frozen=True, slots=True)
class ResolvedIdentity:
    """Which tenant an account belongs to, and whether it was just created."""

    tenant_id: int
    created: bool


class IdentityService:
    """The pre-tenant boundary, and nothing else."""

    def __init__(self, session: AsyncSession, cipher: ColumnCipher, pepper: bytes) -> None:
        self._session = session
        self._cipher = cipher
        self._pepper = pepper

    def hash_of(self, channel: str, external_id: str) -> bytes:
        return identity_hash(channel, external_id, self._pepper)

    async def resolve(self, channel: str, external_id: str) -> int | None:
        """The tenant behind a live identity, or None on first contact."""
        digest = self.hash_of(channel, external_id)
        found: int | None = await self._session.scalar(
            text("SELECT resolve_tenant_for_identity(CAST(:channel AS channel_kind), :digest)"),
            {"channel": channel, "digest": digest},
        )
        return found

    async def resolve_or_create(self, channel: str, external_id: str) -> ResolvedIdentity:
        """Resolve, or create the tenant and its first identity atomically.

        The two are one statement because they cannot be two: the identity's
        policy needs a tenant that does not exist until the first insert
        commits. A concurrent first contact loses the race against
        `ux_channel_identity_active` and resolves instead of duplicating — which
        is the behaviour that matters, since a duplicate would fragment the
        person's history across two tenants (spec 1.3).
        """
        existing = await self.resolve(channel, external_id)
        if existing is not None:
            return ResolvedIdentity(tenant_id=existing, created=False)

        digest = self.hash_of(channel, external_id)
        sealed = self._cipher.encrypt(
            external_id.encode(), identity_aad(channel=channel, external_id_hash=digest)
        )

        try:
            # A savepoint, not the whole transaction. The losing side of a race
            # has to undo its failed insert without touching work the caller
            # had already done in the same transaction — rolling that back was
            # destroying data this function never owned.
            async with self._session.begin_nested():
                tenant_id: int | None = await self._session.scalar(
                    text(
                        "SELECT create_tenant_with_identity("
                        "  CAST(:channel AS channel_kind), :external_id, :digest,"
                        "  CAST(:key_version AS smallint))"
                    ),
                    {
                        "channel": channel,
                        "external_id": sealed,
                        "digest": digest,
                        "key_version": self._cipher.active_version,
                    },
                )
        except IntegrityError as error:
            # `IntegrityError` is all of SQLSTATE class 23 — foreign key, check,
            # not-null, exclusion. Only `23505` means "someone else got here
            # first". The others are real failures, and the difference matters
            # precisely when they coincide with a concurrent create: the
            # follow-up resolve would find that tenant and report success,
            # burying a constraint violation under a plausible answer.
            if getattr(error.orig, "sqlstate", None) != UNIQUE_VIOLATION:
                raise
            concurrent = await self.resolve(channel, external_id)
            if concurrent is None:
                raise
            return ResolvedIdentity(tenant_id=concurrent, created=False)

        if tenant_id is None:  # pragma: no cover - the function cannot return NULL
            raise RuntimeError("create_tenant_with_identity returned no tenant")
        return ResolvedIdentity(tenant_id=tenant_id, created=True)
