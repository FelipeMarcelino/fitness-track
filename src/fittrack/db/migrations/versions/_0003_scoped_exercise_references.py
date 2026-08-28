"""Close the cross-tenant exercise reference (spec 19.1).

Row-level security governs which rows a tenant can *see*. It does not govern
which rows a tenant can *point at*: referential integrity checks run with row
security bypassed, by design — otherwise an invisible row could be deleted out
from under a foreign key.

`exercise_set` had two references to `exercise`: a composite one carrying the
tenant, and a plain `exercise_id` one that did not. The composite is MATCH
SIMPLE, so it is skipped entirely when `exercise_tenant_id` is NULL — which the
CHECK constraint permits, meaning "a global exercise". Nothing then verified
that the exercise actually was global, and the plain FK was happy with any id
at all. So tenant A could record a set against tenant B's private exercise:

  * an existence oracle — a violation means the id is free, success means it
    belongs to someone;
  * and a denial of service — the NO ACTION reference blocks B from ever
    deleting that exercise, with nothing on B's side to explain why.

The fix is to make the tenant part of the reference unskippable. `coalesce`
turns "global" from a NULL into the value 0, which no tenant can hold, so the
composite key never contains a NULL and MATCH SIMPLE always enforces it. The
plain, unscoped foreign keys go away.

`program_milestone` gains the scope column the other two already had; its
`exercise_id` stays nullable, and a NULL there still skips the reference, which
is correct — the milestone simply is not about an exercise.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from fittrack.db.sql import split_statements

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | None = None
depends_on: str | None = None

UPGRADE = """
ALTER TABLE exercise
  ADD COLUMN tenant_scope bigint
  GENERATED ALWAYS AS (coalesce(tenant_id, 0)) STORED;
ALTER TABLE exercise ADD CONSTRAINT uq_exercise_id_scope UNIQUE (id, tenant_scope);

-- exercise_set ------------------------------------------------------------ --
ALTER TABLE exercise_set
  ADD COLUMN exercise_scope bigint
  GENERATED ALWAYS AS (coalesce(exercise_tenant_id, 0)) STORED;
ALTER TABLE exercise_set DROP CONSTRAINT exercise_set_exercise_id_fkey;
ALTER TABLE exercise_set DROP CONSTRAINT exercise_set_exercise_id_exercise_tenant_id_fkey;
ALTER TABLE exercise_set
  ADD CONSTRAINT fk_set_exercise_scoped
  FOREIGN KEY (exercise_id, exercise_scope)
  REFERENCES exercise (id, tenant_scope) ON DELETE CASCADE;

-- plan_item --------------------------------------------------------------- --
ALTER TABLE plan_item
  ADD COLUMN exercise_scope bigint
  GENERATED ALWAYS AS (coalesce(exercise_tenant_id, 0)) STORED;
ALTER TABLE plan_item DROP CONSTRAINT plan_item_exercise_id_fkey;
ALTER TABLE plan_item DROP CONSTRAINT plan_item_exercise_id_exercise_tenant_id_fkey;
ALTER TABLE plan_item
  ADD CONSTRAINT fk_plan_item_exercise_scoped
  FOREIGN KEY (exercise_id, exercise_scope)
  REFERENCES exercise (id, tenant_scope) ON DELETE CASCADE;

-- program_milestone ------------------------------------------------------- --
-- The only one of the three with no scope column at all: its `exercise_id` was
-- a bare reference. It is nullable, and a NULL still skips the key, which is
-- what should happen when the milestone is not about a particular exercise.
ALTER TABLE program_milestone ADD COLUMN exercise_tenant_id bigint;
ALTER TABLE program_milestone
  ADD CONSTRAINT ck_milestone_exercise_scope
  CHECK (exercise_tenant_id IS NULL OR exercise_tenant_id = tenant_id);
ALTER TABLE program_milestone
  ADD COLUMN exercise_scope bigint
  GENERATED ALWAYS AS (coalesce(exercise_tenant_id, 0)) STORED;
ALTER TABLE program_milestone DROP CONSTRAINT program_milestone_exercise_id_fkey;
ALTER TABLE program_milestone
  ADD CONSTRAINT fk_milestone_exercise_scoped
  FOREIGN KEY (exercise_id, exercise_scope)
  REFERENCES exercise (id, tenant_scope) ON DELETE CASCADE;
"""

DOWNGRADE = """
ALTER TABLE program_milestone DROP CONSTRAINT fk_milestone_exercise_scoped;
ALTER TABLE program_milestone DROP COLUMN exercise_scope;
ALTER TABLE program_milestone DROP CONSTRAINT ck_milestone_exercise_scope;
ALTER TABLE program_milestone DROP COLUMN exercise_tenant_id;
ALTER TABLE program_milestone
  ADD CONSTRAINT program_milestone_exercise_id_fkey
  FOREIGN KEY (exercise_id) REFERENCES exercise (id);

ALTER TABLE plan_item DROP CONSTRAINT fk_plan_item_exercise_scoped;
ALTER TABLE plan_item DROP COLUMN exercise_scope;
ALTER TABLE plan_item
  ADD CONSTRAINT plan_item_exercise_id_fkey FOREIGN KEY (exercise_id) REFERENCES exercise (id);
ALTER TABLE plan_item
  ADD CONSTRAINT plan_item_exercise_id_exercise_tenant_id_fkey
  FOREIGN KEY (exercise_id, exercise_tenant_id)
  REFERENCES exercise (id, tenant_id) ON DELETE CASCADE;

ALTER TABLE exercise_set DROP CONSTRAINT fk_set_exercise_scoped;
ALTER TABLE exercise_set DROP COLUMN exercise_scope;
ALTER TABLE exercise_set
  ADD CONSTRAINT exercise_set_exercise_id_fkey FOREIGN KEY (exercise_id) REFERENCES exercise (id);
ALTER TABLE exercise_set
  ADD CONSTRAINT exercise_set_exercise_id_exercise_tenant_id_fkey
  FOREIGN KEY (exercise_id, exercise_tenant_id)
  REFERENCES exercise (id, tenant_id) ON DELETE CASCADE;

ALTER TABLE exercise DROP CONSTRAINT uq_exercise_id_scope;
ALTER TABLE exercise DROP COLUMN tenant_scope;
"""


def _run(script: str) -> None:
    # asyncpg prepares every statement, and a prepared statement holds exactly
    # one command.
    for statement in split_statements(script):
        op.execute(sa.text(statement))


def upgrade() -> None:
    _run(UPGRADE)


def downgrade() -> None:
    _run(DOWNGRADE)
