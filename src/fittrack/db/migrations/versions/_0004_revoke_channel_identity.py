"""Add the least-privilege identity revocation boundary (spec 18.4, 19.1).

The runtime role cannot update ``channel_identity`` directly. Delivery still
has to revoke a blocked, deactivated, or missing destination, so this revision
adds one tenant-qualified operation to the existing security-definer boundary.

Revision ID: 0004
Revises: 0003
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from fittrack.db.sql import split_statements

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | None = None
depends_on: str | None = None

UPGRADE = """
-- The boundary owner may change only the two columns revocation requires.
GRANT UPDATE (revoked_at, is_primary) ON channel_identity TO fittrack_identity;

CREATE FUNCTION revoke_channel_identity(
    p_identity_id bigint,
    p_tenant_id bigint,
    p_revoked_at timestamptz
) RETURNS boolean
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
BEGIN
  UPDATE channel_identity
     SET revoked_at = coalesce(revoked_at, p_revoked_at),
         is_primary = false
   WHERE id = p_identity_id
     AND tenant_id = p_tenant_id;
  RETURN FOUND;
END;
$$;

REVOKE ALL ON FUNCTION revoke_channel_identity(bigint, bigint, timestamptz) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION revoke_channel_identity(bigint, bigint, timestamptz) TO fittrack_app;
ALTER FUNCTION revoke_channel_identity(bigint, bigint, timestamptz)
  OWNER TO fittrack_identity;
"""

DOWNGRADE = """
DROP FUNCTION IF EXISTS revoke_channel_identity(bigint, bigint, timestamptz);
REVOKE UPDATE (revoked_at, is_primary) ON channel_identity FROM fittrack_identity;
"""


def _run(script: str) -> None:
    for statement in split_statements(script):
        op.execute(sa.text(statement))


def upgrade() -> None:
    _run(UPGRADE)


def downgrade() -> None:
    _run(DOWNGRADE)
