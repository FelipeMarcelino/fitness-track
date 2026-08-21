"""Turning a drained burst into a durable unit of work (§4.1, §17.4).

The batch is persisted before the graph runs so a crash mid-processing can be
retried from the database rather than from Redis, which no longer holds the
messages -- the drain took them.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 3


@dataclass(frozen=True)
class Batch:
    id: int
    tenant_id: int
    combined_text: str
    message_ids: list[str]
    attempts: int

    @property
    def exhausted(self) -> bool:
        return self.attempts >= MAX_ATTEMPTS


class BatchStore:
    """Persists batches and tracks their attempts."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def create(self, tenant_id: int, messages: list[dict[str, Any]]) -> Batch | None:
        """Combines a burst into one unit of work.

        Fragments are joined in arrival order and separated, so the extractor
        reads "supino reto | 10kg | 8 reps" as one utterance rather than three.
        """
        texts = [str(m.get("text") or "") for m in messages]
        combined = " | ".join(t for t in texts if t)
        message_ids = [str(m["message_id"]) for m in messages]

        if not message_ids:
            return None

        async with self._engine.begin() as conn:
            await conn.execute(text(f"SET LOCAL app.tenant_id = '{tenant_id}'"))
            row = await conn.execute(
                text(
                    "INSERT INTO processing_batch "
                    "(tenant_id, message_ids, combined_text) "
                    "VALUES (:t, :m, :c) RETURNING id, attempts"
                ),
                {"t": tenant_id, "m": message_ids, "c": combined},
            )
            batch_id, attempts = row.one()

        return Batch(
            id=int(batch_id),
            tenant_id=tenant_id,
            combined_text=combined,
            message_ids=message_ids,
            attempts=int(attempts),
        )

    async def mark_attempt(self, batch: Batch) -> int:
        async with self._engine.begin() as conn:
            await conn.execute(text(f"SET LOCAL app.tenant_id = '{batch.tenant_id}'"))
            row = await conn.execute(
                text(
                    "UPDATE processing_batch SET attempts = attempts + 1 "
                    "WHERE id = :i RETURNING attempts"
                ),
                {"i": batch.id},
            )
            return int(row.scalar_one())

    async def mark_done(self, batch: Batch) -> None:
        await self._finish(batch, "done", None)

    async def mark_failed(self, batch: Batch, error: str) -> None:
        """Terminal. The user is told rather than left in silence (§7.3), and
        the original text stays in raw_message either way."""
        await self._finish(batch, "failed", error)

    async def _finish(self, batch: Batch, status: str, error: str | None) -> None:
        async with self._engine.begin() as conn:
            await conn.execute(text(f"SET LOCAL app.tenant_id = '{batch.tenant_id}'"))
            await conn.execute(
                text(
                    "UPDATE processing_batch "
                    "SET status = :s, error = :e, finished_at = now() WHERE id = :i"
                ),
                {"s": status, "e": error, "i": batch.id},
            )
