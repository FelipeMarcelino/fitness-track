"""The migration must be reversible, so a bad deploy can be rolled back."""

from __future__ import annotations

import os

import pytest
from alembic import command
from alembic.config import Config

pytestmark = pytest.mark.integration


def test_upgrade_downgrade_upgrade(postgres_dsn: str) -> None:
    """A downgrade that leaves debris makes the next upgrade fail, and that is
    discovered during an incident rather than here."""
    os.environ["ALEMBIC_DSN"] = postgres_dsn
    cfg = Config("alembic.ini")
    try:
        command.upgrade(cfg, "head")
        command.downgrade(cfg, "base")
        command.upgrade(cfg, "head")
    finally:
        os.environ.pop("ALEMBIC_DSN", None)
