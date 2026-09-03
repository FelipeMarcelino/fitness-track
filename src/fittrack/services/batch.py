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

NOTE: The ARQ entry points and ``max_tries=3`` registration live in
``worker.py``.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Protocol

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from fittrack.db.engine import tenant_session
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

    At the wiring boundary (worker.py), a ``PostgresBatchStore`` backed by
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

    async def find_by_message_ids(
        self, *, tenant_id: int, message_ids: list[str]
    ) -> BatchRow | None:
        """Find the batch already persisted for the same Redis drain."""
        ...

    async def get(self, *, batch_id: int, tenant_id: int) -> BatchRow | None:
        """Load a batch row, or ``None`` if not found."""
        ...

    async def mark_done(self, *, batch_id: int, tenant_id: int) -> None:
        """Set ``status='done'``, ``finished_at=now()``, ``attempts += 1``."""
        ...


class PostgresBatchStore:
    """Tenant-scoped PostgreSQL implementation of :class:`BatchStore`."""

    _SELECT_COLUMNS = "id, tenant_id, message_ids, combined_text, key_version, status, attempts"

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def reserve_id(self) -> int:
        async with self._sessions() as session, session.begin():
            row_id = await session.scalar(text("SELECT nextval('processing_batch_id_seq')"))
        if row_id is None:  # pragma: no cover - PostgreSQL sequences never return NULL
            raise RuntimeError("processing_batch_id_seq returned no id")
        return int(row_id)

    async def insert(
        self,
        *,
        row_id: int,
        tenant_id: int,
        message_ids: list[str],
        combined_text: bytes,
        key_version: int,
    ) -> None:
        async with tenant_session(self._sessions, tenant_id) as session:
            await session.execute(
                text(
                    "INSERT INTO processing_batch ("
                    "id, tenant_id, message_ids, combined_text, key_version"
                    ") VALUES ("
                    ":id, :tenant_id, CAST(:message_ids AS TEXT[]), :combined_text, :key_version"
                    ")"
                ),
                {
                    "id": row_id,
                    "tenant_id": tenant_id,
                    "message_ids": message_ids,
                    "combined_text": combined_text,
                    "key_version": key_version,
                },
            )

    async def find_by_message_ids(
        self, *, tenant_id: int, message_ids: list[str]
    ) -> BatchRow | None:
        async with tenant_session(self._sessions, tenant_id) as session:
            result = await session.execute(
                text(
                    f"SELECT {self._SELECT_COLUMNS} FROM processing_batch "
                    "WHERE message_ids = CAST(:message_ids AS TEXT[]) "
                    "ORDER BY id DESC LIMIT 1"
                ),
                {"message_ids": message_ids},
            )
            row = result.mappings().one_or_none()
        return self._to_batch_row(row) if row is not None else None

    async def get(self, *, batch_id: int, tenant_id: int) -> BatchRow | None:
        async with tenant_session(self._sessions, tenant_id) as session:
            result = await session.execute(
                text(f"SELECT {self._SELECT_COLUMNS} FROM processing_batch WHERE id = :batch_id"),
                {"batch_id": batch_id},
            )
            row = result.mappings().one_or_none()
        return self._to_batch_row(row) if row is not None else None

    async def mark_done(self, *, batch_id: int, tenant_id: int) -> None:
        async with tenant_session(self._sessions, tenant_id) as session:
            await session.execute(
                text(
                    "UPDATE processing_batch "
                    "SET status = 'done', finished_at = now(), attempts = attempts + 1 "
                    "WHERE id = :batch_id AND status = 'pending'"
                ),
                {"batch_id": batch_id},
            )

    @staticmethod
    def _to_batch_row(row: object) -> BatchRow:
        from collections.abc import Mapping

        if not isinstance(row, Mapping):  # pragma: no cover - SQLAlchemy contract
            raise TypeError("processing_batch query did not return a mapping")
        return BatchRow(
            id=int(row["id"]),
            tenant_id=int(row["tenant_id"]),
            message_ids=[str(value) for value in row["message_ids"]],
            combined_text=bytes(row["combined_text"]),
            key_version=int(row["key_version"]),
            status=str(row["status"]),
            attempts=int(row["attempts"]),
        )


class VoiceResolver(Protocol):
    """The transcription step of the drain (S02-T07).

    Declared as a port rather than imported concretely so that a drain with no
    voice in it — which is most of them — costs nothing and needs no provider
    credential. `VoiceIngestion` in ``services/stt.py`` is the implementation.
    """

    async def resolve(
        self, items: list[dict[str, object]], *, tenant_id: int
    ) -> list[dict[str, object]]:
        """Return the burst with its voice items turned into text."""
        ...


class BatchEnqueuer(Protocol):
    """Enqueue a ``process_batch`` job without exposing ARQ.

    Deferring a batch is deliberately not part of this surface: it happens
    from inside the batch job itself, where the job identity ARQ allows is a
    different one, and that is a queue concern (``worker.py``).
    """

    async def enqueue_process_batch(self, *, tenant_id: int, batch_id: int) -> None: ...


# ─── Pure helpers ──────────────────────────────────────────────────────────


def prepare_items(
    items: list[dict[str, object]],
) -> tuple[list[dict[str, object]], list[str]]:
    """Mark voice items ``was_audio=True`` and extract message ids.

    Preserves arrival order (§4.1).  Items without ``raw_message_id`` are
    kept in the list but excluded from ``message_ids``.

    A voice item that reaches this point without text was never transcribed —
    either no resolver was wired (a deployment with no STT credential, see
    ``worker.build_voice_ingestion``) or the resolver failed. Either way it
    leaves here in the shape the rest of the pipeline expects from a failed
    transcription: empty text, ``status='incomplete'`` so it stays outside
    every analysis (invariant 6), and no ``media_ref``, which nothing
    downstream reads and which is a reusable channel access reference (§20.6).
    """
    message_ids: list[str] = []
    for item in items:
        if item.get("kind") == "voice":
            item["was_audio"] = True
            if not item.get("text"):
                item["text"] = ""
                item["status"] = "incomplete"
            item["media_ref"] = None
        # This is globally unique across channels and remains stable when an
        # orphaned Redis drain is retried.
        msg_id = item.get("raw_message_id")
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
    voice: VoiceResolver | None = None,
) -> int | None:
    """Persist a ``processing_batch`` with encrypted ``combined_text``.

    Returns the batch row id, or ``None`` when the drain left nothing to
    process.  Voice items are transcribed and marked ``was_audio=True``
    before the combined text is encrypted (S02-T05 and S02-T07): the batch the
    graph receives holds text, never a media reference.

    A burst can empty here, and legitimately: a lone voice note that was
    inaudible, too long or unconsented was answered with the fixed reply of
    §11.3, and an empty batch would only give the graph a turn with no content
    in it.
    """
    items = drain.items
    if voice is not None:
        items = await voice.resolve(items, tenant_id=tenant_id)
    items, message_ids = prepare_items(items)

    # The drain is acknowledged only after enqueue succeeds. If enqueue failed
    # after the insert, the next flush retry repairs the handoff by reusing the
    # row identified by the same ordered raw-message ids.
    if message_ids:
        existing = await store.find_by_message_ids(
            tenant_id=tenant_id,
            message_ids=message_ids,
        )
        if existing is not None:
            logger.info(
                "batch already persisted, reusing",
                extra={"tenant_id": tenant_id, "batch_id": existing.id},
            )
            return existing.id

    if not items:
        logger.info(
            "drain left no item to process",
            extra={"tenant_id": tenant_id},
        )
        return None

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


class BatchLockContentionError(RuntimeError):
    """The tenant lock is busy and ARQ should defer this batch job."""


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
    ``WorkerSettings`` configures ``keep_result=0``.
    """
    async with tenant_lock(redis, tenant_id) as token:
        if token is None:
            raise BatchLockContentionError

        row = await store.get(batch_id=batch_id, tenant_id=tenant_id)
        if row is None:
            logger.warning(
                "batch not found",
                extra={"tenant_id": tenant_id, "batch_id": batch_id},
            )
            return

        if row.status != "pending":
            logger.info(
                "batch is terminal, skipping",
                extra={
                    "tenant_id": tenant_id,
                    "batch_id": batch_id,
                    "batch_status": row.status,
                },
            )
            return

        # Sprint 03: graph execution goes here (ainvoke)

        await store.mark_done(batch_id=batch_id, tenant_id=tenant_id)

        logger.info(
            "batch handoff complete",
            extra={"tenant_id": tenant_id, "batch_id": batch_id},
        )
