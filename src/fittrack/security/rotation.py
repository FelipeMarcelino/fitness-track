"""Rotating the identity pepper (spec 22.2).

It cannot be a progressive, dual-read rotation the way a key rotation is. Two
peppers produce two hashes for one account, and both would slip past
`ux_channel_identity_active` — so the same person could resolve to two tenants,
which is the single failure that table exists to prevent.

So it is an atomic maintenance instead, and the ordering is the whole procedure:

1. pause the ingress (the operator's step, outside this module);
2. lock `channel_identity` so nothing writes while the hashes are inconsistent;
3. for every row, decrypt under the old associated data, rehash under the new
   pepper, re-encrypt under the new associated data — the hash is *part* of that
   associated data, so the ciphertext has to be rewritten too;
4. commit;
5. only then swap `FITTRACK_IDENTITY_PEPPER` and resume traffic.

A failure at step 3 rolls the whole transaction back, leaving the table exactly
as it was — which is what makes a retry safe, and why the secret must not be
swapped first.
"""

from __future__ import annotations

from dataclasses import dataclass

import asyncpg

from fittrack.security.crypto import ColumnCipher, identity_aad
from fittrack.security.identity_hash import _require_pepper, identity_hash


class RotationError(RuntimeError):
    """The table is not in a state this rotation can safely rewrite."""


@dataclass(frozen=True, slots=True)
class PepperRotation:
    """What one rotation did. `scanned` and `rewritten` differing is a bug."""

    scanned: int
    rewritten: int


async def rotate_pepper(
    connection: asyncpg.Connection,
    *,
    cipher: ColumnCipher,
    old_pepper: bytes,
    new_pepper: bytes,
) -> PepperRotation:
    """Rehash and re-encrypt every identity, in one transaction.

    Both peppers are checked before the table is locked: discovering a short one
    afterwards would hold an ACCESS EXCLUSIVE lock for no reason.
    """
    _require_pepper(old_pepper)
    _require_pepper(new_pepper)

    scanned = 0
    rewritten = 0

    async with connection.transaction():
        # ACCESS EXCLUSIVE: for the duration, the hashes in this table are being
        # rewritten, and a concurrent ingress lookup would miss a row that is
        # about to change — then create a second tenant for the same account.
        await connection.execute("LOCK TABLE channel_identity IN ACCESS EXCLUSIVE MODE")

        rows = await connection.fetch(
            "SELECT id, channel, external_id, external_id_hash FROM channel_identity"
        )
        for row in rows:
            scanned += 1
            channel = row["channel"]
            old_hash = bytes(row["external_id_hash"])

            # Fails closed on the wrong pepper, an already-rotated row under a
            # different one, or a tampered blob — and the transaction unwinds.
            external_id = cipher.decrypt(
                bytes(row["external_id"]),
                identity_aad(channel=channel, external_id_hash=old_hash),
            ).decode()

            # The old pepper is not needed to *decrypt* — the associated data
            # comes from the stored hash — but it is needed to know the row is
            # where the caller thinks it is. A hash that does not match under
            # the old pepper means this row was written under some third one:
            # a rotation that stopped halfway. Continuing would rewrite it into
            # a state nobody can account for, so the transaction unwinds.
            if identity_hash(channel, external_id, old_pepper) != old_hash:
                raise RotationError(
                    f"identity {row['id']} does not hash to its stored value under the "
                    "old pepper: an earlier rotation did not finish"
                )

            new_hash = identity_hash(channel, external_id, new_pepper)
            await connection.execute(
                """
                UPDATE channel_identity
                   SET external_id = $2, external_id_hash = $3, key_version = $4
                 WHERE id = $1
                """,
                row["id"],
                cipher.encrypt(
                    external_id.encode(),
                    identity_aad(channel=channel, external_id_hash=new_hash),
                ),
                new_hash,
                cipher.active_version,
            )
            rewritten += 1

    return PepperRotation(scanned=scanned, rewritten=rewritten)
