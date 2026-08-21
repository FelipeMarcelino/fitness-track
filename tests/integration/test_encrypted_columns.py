"""The §22.2 columns must hold ciphertext, not text.

A unit test proves the cipher works; only a database can prove that what
actually landed in the column is unreadable.
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from fittrack.crypto.aesgcm import Encryptor, KeyRing

pytestmark = pytest.mark.integration

ENCRYPTOR = Encryptor(KeyRing({1: os.urandom(32)}, current_version=1))


async def test_health_report_stores_ciphertext(owner_conn: AsyncConnection) -> None:
    """The verbatim complaint is article 11 data. If this ever reads back as
    the original string, something wrote plaintext."""
    row = await owner_conn.execute(
        text("INSERT INTO tenant (bsuid) VALUES ('crypto') RETURNING id")
    )
    tenant_id = int(row.scalar_one())
    complaint = "dor aguda no ombro direito ao subir carga"
    blob, version = ENCRYPTOR.encrypt(complaint)

    await owner_conn.execute(
        text(
            "INSERT INTO health_report (tenant_id, category, verbatim, key_version) "
            "VALUES (:t, 'dor', :v, :k)"
        ),
        {"t": tenant_id, "v": blob, "k": version},
    )

    stored = await owner_conn.execute(
        text("SELECT verbatim, key_version FROM health_report WHERE tenant_id = :t"),
        {"t": tenant_id},
    )
    raw, key_version = stored.one()

    assert complaint.encode() not in bytes(raw), "plaintext reached the column"
    assert ENCRYPTOR.decrypt(bytes(raw), key_version) == complaint


async def test_body_metric_value_is_not_aggregable(
    owner_conn: AsyncConnection,
) -> None:
    """The consequence recorded in §22.2: an encrypted column cannot be summed
    in SQL, which is why body_metric_trend computes in Python. This test exists
    so the constraint is discovered here rather than in the analytics tool."""
    row = await owner_conn.execute(text("INSERT INTO tenant (bsuid) VALUES ('nosum') RETURNING id"))
    tenant_id = int(row.scalar_one())
    blob, version = ENCRYPTOR.encrypt("82.4")
    await owner_conn.execute(
        text(
            "INSERT INTO body_metric "
            "(tenant_id, local_date, kind, value, unit, key_version) "
            "VALUES (:t, CURRENT_DATE, 'peso', :v, 'kg', :k)"
        ),
        {"t": tenant_id, "v": blob, "k": version},
    )

    with pytest.raises(Exception, match=r"function avg|does not exist|bytea"):
        await owner_conn.execute(
            text("SELECT avg(value) FROM body_metric WHERE tenant_id = :t"),
            {"t": tenant_id},
        )


async def test_every_column_listed_in_the_spec_is_bytea(
    owner_conn: AsyncConnection,
) -> None:
    """Guards against a future migration relaxing one back to text."""
    expected = {
        ("health_report", "verbatim"),
        ("body_metric", "value"),
        ("athlete_profile", "injuries"),
        ("raw_message", "payload"),
        ("raw_message", "transcript"),
        ("session_summary", "narrative"),
    }
    rows = await owner_conn.execute(
        text(
            "SELECT table_name, column_name, data_type FROM information_schema.columns "
            "WHERE table_schema = 'public'"
        )
    )
    types = {(r[0], r[1]): r[2] for r in rows}

    wrong = {c for c in expected if types[c] != "bytea"}
    assert not wrong, f"not encrypted: {sorted(wrong)}"
