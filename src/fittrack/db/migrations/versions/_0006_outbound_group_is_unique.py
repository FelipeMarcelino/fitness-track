"""One row per bubble of one answer (spec 18.2, 5.2).

`outbound_queue` already describes itself this way — "bubbles of one answer
share `group_id` and go out in `seq` order" — and the code has always produced
one row per pair. This makes the database say it too, which is what turns a
deterministic `group_id` into an idempotent enqueue.

The fixed replies of §11.3 need that. They are queued in one transaction and
marked answered in another, so a failure between the two brings the same
refusal round again; with the group derived from the message it answers
(`_reply_group` in `services/stt.py`), the second insert is a no-op instead of
a second bubble at the user.

Revision ID: 0006
Revises: 0005
"""

from __future__ import annotations

from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_index(
        "ux_outbound_group_seq",
        "outbound_queue",
        ["group_id", "seq"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("ux_outbound_group_seq", table_name="outbound_queue")
