"""Batch drain and persistence (S02-T05).

Spec: §4 (end-to-end flow), §17.2 (fila default), §4.1 (guarantees),
§22.2 (combined_text encrypted).

After ``flush_check`` drains a tenant buffer (S02-T04), this module
persists the ``processing_batch`` with encrypted ``combined_text`` and
provides the ``process_batch`` handler that marks the batch done.

The ``process_batch`` handler acquires a per-tenant lock before touching
the batch, so that Sprint 03's graph execution remains serialised per
tenant (§4.1).  In this sprint it is a placeholder: it marks the batch
``done`` and logs the handoff.

NOTE: The ARQ entry points (enqueue and handler) live in ``worker.py``.
The ``max_tries=3`` and exponential backoff configuration is completed
in S02-T08 (WorkerSettings wiring).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Protocol

from fittrack.security.crypto import ColumnCipher, column_aad
from fittrack.services.debounce import DrainResult, RedisWorkerClient, tenant_lock

logger = logging.getLogger(__name__)


# ─── Value objects ──────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class BatchRow:
    """A ``processing_batch`` row as seen by the service layer."""

    id: int
    tenant_id: int
    message_ids: list[str]
    combined_text: bytes
    key_version: int
    status: str
    attempts: int


# ─── Ports ──────────────────────────────────────────────────────────────────


class BatchStore(Protocol):
    """The database surface for ``processing_batch`` operations.

    At the wiring boundary (worker.py), a ``PgBatchStore`` backed by
    ``tenant_session`` is passed.  Tests use a ``FakeBatchStore``.
    """

    async def reserve_id(self) -> int:
        """Reserve the next ``BIGSERIAL`` id.

        Called before encryption so the row id is part of the AAD
        (spec §22.2).
        """
        ...

    async def insert(
        self,
        *,
        row_id: int,
        tenant_id: int,
        message_ids: list[str],
        combined_text: bytes,
        key_version: int,
    ) -> None:
        """Insert a ``processing_batch`` row with the reserved id."""
        ...

    async def get(self, *, batch_id: int, tenant_id: int) -> BatchRow | None:
        """Load a batch row, or ``None`` if not found."""
        ...

    async def mark_done(self, *, batch_id: int, tenant_id: int) -> None:
        """Set ``status='done'``, ``finished_at=now()``, ``attempts += 1``."""
        ...


class BatchEnqueuer(Protocol):
    """Enqueue a ``process_batch`` job without exposing ARQ."""

    async def enqueue_process_batch(self, *, tenant_id: int, batch_id: int) -> None: ...


# ─── Pure helpers ──────────────────────────────────────────────────────────


def prepare_items(
    items: list[dict[str, object]],
) -> tuple[list[dict[str, object]], list[str]]:
    """Mark voice items ``was_audio=True`` and extract message ids.

    Preserves arrival order (§4.1).  Items without ``message_id`` are
    kept in the list but excluded from ``message_ids``.
    """
    message_ids: list[str] = []
    for item in items:
        if item.get("kind") == "voice":
            item["was_audio"] = True
        msg_id = item.get("message_id")
        if msg_id is not None:
            message_ids.append(str(msg_id))
    return items, message_ids


# ─── Persist ───────────────────────────────────────────────────────────────


async def persist_batch(
    *,
    drain: DrainResult,
    tenant_id: int,
    cipher: ColumnCipher,
    store: BatchStore,
) -> int:
    """Persist a ``processing_batch`` with encrypted ``combined_text``.

    Returns the batch row id.  Voice items are marked ``was_audio=True``
    before the combined text is encrypted (acceptance criterion T05).
    """
    items, message_ids = prepare_items(drain.items)

    # JSON-encode items in arrival order — no concatenation (§9.3)
    combined_bytes = json.dumps(items, ensure_ascii=False).encode()

    # Reserve BIGSERIAL, then encrypt (§22.2: AAD needs row_id)
    row_id = await store.reserve_id()
    aad = column_aad(
        tenant_id=tenant_id,
        table="processing_batch",
        column="combined_text",
        row_id=row_id,
    )
    encrypted = cipher.encrypt(combined_bytes, aad)

    await store.insert(
        row_id=row_id,
        tenant_id=tenant_id,
        message_ids=message_ids,
        combined_text=encrypted,
        key_version=cipher.active_version,
    )

    logger.info(
        "batch persisted",
        extra={"tenant_id": tenant_id, "batch_id": row_id, "item_count": len(items)},
    )
    return row_id


# ─── Process ───────────────────────────────────────────────────────────────


async def process_batch(
    *,
    tenant_id: int,
    batch_id: int,
    redis: RedisWorkerClient,
    store: BatchStore,
) -> None:
    """Process a persisted batch.

    Acquires the per-tenant lock (§4.1), loads the batch, and marks it
    ``done``.  This is the handoff point for Sprint 03's graph execution:
    the lock is already held and the batch is loaded — the ``ainvoke``
    call goes here.

    Registered with ``max_tries=3`` and exponential backoff (§4.1) so
    that a lock contention or a transient DB error is retried by ARQ.
    The ``keep_result=0`` setting is completed in S02-T08.
    """
    async with tenant_lock(redis, tenant_id) as token:
        if token is None:
            raise RuntimeError(f"could not acquire lock for tenant {tenant_id}")

        row = await store.get(batch_id=batch_id, tenant_id=tenant_id)
        if row is None:
            logger.warning(
                "batch not found",
                extra={"tenant_id": tenant_id, "batch_id": batch_id},
            )
            return

        if row.status == "done":
            logger.info(
                "batch already done, skipping",
                extra={"tenant_id": tenant_id, "batch_id": batch_id},
            )
            return

        # Sprint 03: graph execution goes here (ainvoke)

        await store.mark_done(batch_id=batch_id, tenant_id=tenant_id)

        logger.info(
            "batch handoff complete",
            extra={"tenant_id": tenant_id, "batch_id": batch_id},
        )
