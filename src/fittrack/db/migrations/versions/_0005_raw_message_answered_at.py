"""Separate "answered with a fixed reply" from "processed" (spec 11.3, 18.4).

A voice note that is refused — inaudible, past the duration ceiling, or without
`workout_data` consent — gets one fixed reply and must not get a second when
the drain behind it is retried. That needs a durable marker, and the marker has
to mean exactly one thing.

``processed_at`` cannot be it. The graph of Sprint 03 is what finishes a
message, and it will write that column; a fixed reply keyed on it would then be
suppressed by unrelated bookkeeping. The reverse happened first and is why this
revision exists: transcription stamped ``processed_at``, and a successful
transcription whose batch failed to persist silently swallowed the consent
reply on the retry.

Additive and nullable, so it applies to a live table without a rewrite. See
ADR-0008.

Revision ID: 0005
Revises: 0004
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    # No grant: the initial revision gives the application role table-level
    # UPDATE on everything in the schema, which covers a column added later.
    op.add_column(
        "raw_message",
        sa.Column("answered_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("raw_message", "answered_at")
