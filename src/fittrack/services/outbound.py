"""The outbound queue: ordered delivery with a persisted position (§13.6, §18.5).

A reply is split into bubbles so it reads like someone typing rather than a
wall of text. That split creates an ordering problem the sender does not have:
bubble 2 delivered before bubble 1 is a non-sequitur followed by its own setup,
and bubble 1 delivered without bubble 2 is a question the user never sees
answered.

Both are solved the same way -- the delivery position lives in the database,
not in the dispatcher. That is what makes a restart resume exactly where it
stopped instead of replaying the prefix, and it is why `sent_at` on the
previous bubble is the eligibility condition rather than an in-memory cursor.

Claiming bumps `attempts` and pushes `next_retry_at` forward by a lease. A
dispatcher that dies mid-send therefore costs one attempt and the bubble
returns on its own; the alternative -- holding the row locked across the HTTP
call -- ties a database transaction to Meta's latency.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from typing import Any, Final
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from fittrack.services.retry_policy import Action, Decision, backoff_seconds

log = logging.getLogger(__name__)

# How long a dispatcher is assumed to still be sending a claimed bubble.
# Longer than any realistic Cloud API call, short enough that a crash does not
# strand the rest of the reply for minutes.
CLAIM_LEASE_SECONDS: Final = 60

# How long an out-of-window bubble waits when there is no template to convert
# to. The window reopens the moment the user writes again, and §18.5 says to
# defer rather than give up -- so this is a ceiling, not a schedule.
WINDOW_WAIT_SECONDS: Final = 24 * 3600

# The Cloud API code for "the 24h window closed" (§18.5). Named because the
# inbound path looks for exactly these rows when the window reopens.
OUT_OF_WINDOW: Final = "131047"

# Kinds that have a template equivalent (§18.5). Media and interactive bubbles
# do not: a template with an image header needs an approved template per image,
# which is not something the runtime can conjure.
TEMPLATE_EQUIVALENT: Final[frozenset[str]] = frozenset({"text"})


@dataclass(frozen=True)
class Bubble:
    """One message of a reply, before it has a row."""

    kind: str
    payload: dict[str, Any]


@dataclass(frozen=True)
class QueuedBubble:
    """A row of outbound_queue."""

    id: int
    tenant_id: int
    group_id: UUID
    seq: int
    kind: str
    payload: dict[str, Any]
    attempts: int
    sent_at: object | None = None
    dead_at: object | None = None
    next_retry_at: object | None = None
    error_code: str | None = None
    retryable: bool | None = None
    last_error: str | None = None
    extras: dict[str, Any] = field(default_factory=dict)


# The §18.5 eligibility query. Two conditions carry the weight: `scheduled_at`
# answers "may this go out at all yet", `next_retry_at` answers "may it be
# tried again yet", and a bubble is eligible only when both have passed.
# NOT EXISTS is the ordering guarantee -- an ORDER BY alone would let a second
# dispatcher take seq=1 while the first is still sending seq=0.
# Between groups the order is insertion order. Ordering by group_id would sort
# by a random UUID, so a reply queued later could overtake one queued first.
_NEXT_ELIGIBLE: Final = """
SELECT q.id, q.tenant_id, q.group_id, q.seq, q.kind, q.payload, q.attempts
  FROM outbound_queue q
 WHERE q.sent_at IS NULL AND q.dead_at IS NULL
   AND q.scheduled_at <= CAST(:now AS timestamptz)
   AND q.next_retry_at <= CAST(:now AS timestamptz)
   AND NOT EXISTS (
        SELECT 1 FROM outbound_queue prev
         WHERE prev.group_id = q.group_id
           AND prev.seq < q.seq
           AND prev.sent_at IS NULL
           AND prev.dead_at IS NULL)
 ORDER BY q.id
 LIMIT 1
 FOR UPDATE SKIP LOCKED
"""


class OutboundQueue:
    """Persists replies and hands them out one bubble at a time."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def enqueue(self, tenant_id: int, bubbles: list[Bubble]) -> UUID:
        """Queues a whole reply under one group_id.

        The bubbles are written in a single transaction: a partially queued
        reply would start going out and then be missing its ending, which is
        the failure the group exists to prevent.
        """
        group_id = uuid4()
        async with self._engine.begin() as conn:
            await self._scope(conn, tenant_id)
            for seq, bubble in enumerate(bubbles):
                await conn.execute(
                    text(
                        "INSERT INTO outbound_queue "
                        "(tenant_id, kind, payload, group_id, seq) "
                        "VALUES (:t, :k, CAST(:p AS jsonb), :g, :s)"
                    ),
                    {
                        "t": tenant_id,
                        "k": bubble.kind,
                        "p": _json(bubble.payload),
                        "g": str(group_id),
                        "s": seq,
                    },
                )
        return group_id

    async def claim_next(self, tenant_id: int, now_offset: int = 0) -> QueuedBubble | None:
        """Takes this tenant's next eligible bubble, or None.

        Scoped to one tenant rather than sweeping the whole table, because RLS
        is FORCEd per tenant (§19.1) and a cross-tenant scan would see nothing.
        That is the right shape anyway: ordering is per reply, so a dispatcher
        is woken for a user the same way `flush_user` is, and a backoff
        reschedules that user's job rather than a global poll.

        `now_offset` shifts the clock forward in seconds. Tests use it to step
        past a backoff without sleeping through it; production leaves it at 0.

        The attempt is counted here rather than after the send, so a dispatcher
        that dies mid-call still spends one. Counting afterwards means a send
        that reliably crashes the process retries forever.
        """
        async with self._engine.begin() as conn:
            await self._scope(conn, tenant_id)
            row = await conn.execute(text(_NEXT_ELIGIBLE), {"now": _now(now_offset)})
            found = row.one_or_none()
            if found is None:
                return None

            bubble = QueuedBubble(
                id=int(found.id),
                tenant_id=int(found.tenant_id),
                group_id=UUID(str(found.group_id)),
                seq=int(found.seq),
                kind=str(found.kind),
                payload=dict(found.payload),
                attempts=int(found.attempts) + 1,
            )
            # Lease it: the row stays claimable if we die, but not before the
            # send could plausibly have finished.
            await conn.execute(
                text(
                    "UPDATE outbound_queue SET attempts = attempts + 1, "
                    "next_retry_at = CAST(:now AS timestamptz) "
                    "+ make_interval(secs => CAST(:lease AS double precision)) "
                    "WHERE id = :i"
                ),
                {"i": bubble.id, "now": _now(now_offset), "lease": CLAIM_LEASE_SECONDS},
            )
        return bubble

    async def mark_sent(self, bubble: QueuedBubble, provider_message_id: str) -> None:
        """Records delivery and unblocks the next bubble of the group."""
        async with self._engine.begin() as conn:
            await self._scope(conn, bubble.tenant_id)
            await conn.execute(
                text(
                    "UPDATE outbound_queue SET sent_at = now(), error_code = NULL, "
                    "last_error = NULL, "
                    "payload = payload || jsonb_build_object('wa_message_id', CAST(:w AS text)) "
                    "WHERE id = :i"
                ),
                {"i": bubble.id, "w": provider_message_id},
            )

    async def mark_failed(
        self,
        bubble: QueuedBubble,
        decision: Decision,
        error: str,
        code: str | None = None,
    ) -> float | None:
        """Applies the §18.5 policy to a failed send.

        `code` is recorded on the row because the daily dead-letter report
        groups by it: one error class growing is a change in Meta's behaviour,
        not a run of bad luck, and that is invisible without the code.

        Returns the backoff it chose, or None when the bubble will not be
        retried. The caller needs the same number: the delay is jittered, so
        drawing it twice would let the wake-up job fire before the row is
        eligible -- it would find nothing, send nothing, and reschedule
        nothing, stranding the bubble until something else woke the tenant.
        """
        bubble = replace(bubble, error_code=code or bubble.error_code)
        if decision.action is Action.RETRY and bubble.attempts < decision.max_attempts:
            return await self._reschedule(bubble, decision, error)

        if decision.action is Action.TEMPLATE:
            await self._to_template(bubble, error)
            return None

        if decision.action is Action.SUSPEND:
            # The recipient, not the message. Every other message to them fails
            # identically, so proactive sends stop for the whole tenant.
            await self._suspend(bubble.tenant_id)

        if decision.action is Action.ALERT:
            # Nothing downstream can fix an account block or our own malformed
            # payload; an operator has to see it.
            log.error(
                "outbound bubble %s died: %s (group %s, tenant %s)",
                bubble.id,
                error,
                bubble.group_id,
                bubble.tenant_id,
            )

        await self.kill_group(bubble, error)
        return None

    async def kill_group(self, bubble: QueuedBubble, error: str) -> None:
        """Gives up on this bubble and everything after it in the reply.

        Half a reply is worse than none: the remaining bubbles read as a
        fragment answering something the user never saw asked, and nothing
        tells them a piece is missing.
        """
        async with self._engine.begin() as conn:
            await self._scope(conn, bubble.tenant_id)
            await conn.execute(
                text(
                    "UPDATE outbound_queue SET dead_at = now(), retryable = false, "
                    "error_code = COALESCE(error_code, :c), last_error = :e "
                    "WHERE group_id = :g AND sent_at IS NULL AND dead_at IS NULL"
                ),
                {"g": str(bubble.group_id), "e": error[:1000], "c": bubble.error_code},
            )
            await conn.execute(
                text("UPDATE outbound_queue SET error_code = :c, last_error = :e WHERE id = :i"),
                {"i": bubble.id, "c": bubble.error_code, "e": error[:1000]},
            )

    async def get(self, tenant_id: int, bubble_id: int) -> QueuedBubble | None:
        async with self._engine.begin() as conn:
            await self._scope(conn, tenant_id)
            row = await conn.execute(
                text(
                    "SELECT id, tenant_id, group_id, seq, kind, payload, attempts, "
                    "sent_at, dead_at, next_retry_at, error_code, retryable, last_error "
                    "FROM outbound_queue WHERE id = :i"
                ),
                {"i": bubble_id},
            )
            found = row.one_or_none()
        return None if found is None else _row(found)

    async def group(self, tenant_id: int, group_id: UUID) -> list[QueuedBubble]:
        async with self._engine.begin() as conn:
            await self._scope(conn, tenant_id)
            rows = await conn.execute(
                text(
                    "SELECT id, tenant_id, group_id, seq, kind, payload, attempts, "
                    "sent_at, dead_at, next_retry_at, error_code, retryable, last_error "
                    "FROM outbound_queue WHERE group_id = :g ORDER BY seq"
                ),
                {"g": str(group_id)},
            )
            return [_row(row) for row in rows.all()]

    async def _reschedule(self, bubble: QueuedBubble, decision: Decision, error: str) -> float:
        delay = backoff_seconds(decision, attempt=bubble.attempts)
        async with self._engine.begin() as conn:
            await self._scope(conn, bubble.tenant_id)
            await conn.execute(
                text(
                    "UPDATE outbound_queue SET next_retry_at = now() + "
                    "make_interval(secs => CAST(:d AS double precision)), error_code = :c, retryable = true, "
                    "last_error = :e WHERE id = :i"
                ),
                {"i": bubble.id, "d": delay, "c": bubble.error_code, "e": error[:1000]},
            )
        return delay

    async def _to_template(self, bubble: QueuedBubble, error: str) -> None:
        """Handles 131047, where the 24h window closed mid-reply.

        A template is the only thing Meta will accept outside the window, and
        only text bubbles have an equivalent: a template with a media header
        needs its own approved template, which the runtime cannot invent. What
        cannot be converted is parked rather than killed -- the user writing
        again is all it takes, and `release_parked` is what notices. The delay
        set here is a backstop for the case where they never do, not a
        prediction of when the window reopens.
        """
        convertible = bubble.kind in TEMPLATE_EQUIVALENT
        async with self._engine.begin() as conn:
            await self._scope(conn, bubble.tenant_id)
            await conn.execute(
                text(
                    "UPDATE outbound_queue SET kind = :k, retryable = false, "
                    "error_code = :c, last_error = :e, "
                    "next_retry_at = now() + make_interval(secs => CAST(:d AS double precision)), "
                    "scheduled_at = now() + make_interval(secs => CAST(:d AS double precision)) "
                    "WHERE id = :i"
                ),
                {
                    "i": bubble.id,
                    "k": "template" if convertible else bubble.kind,
                    "c": bubble.error_code,
                    "e": error[:1000],
                    "d": WINDOW_WAIT_SECONDS,
                },
            )

    async def release_parked(self, tenant_id: int) -> int:
        """Makes out-of-window bubbles eligible again, and says how many.

        A bubble parked by 131047 is waiting for something no timer can
        predict: the user writing again. Parking it on a fixed delay means a
        message that reopens the window five minutes later leaves the reply
        sitting there for the rest of the day. So the inbound path calls this
        instead, and the ceiling in `_to_template` is only a backstop.
        """
        async with self._engine.begin() as conn:
            await self._scope(conn, tenant_id)
            result = await conn.execute(
                text(
                    "UPDATE outbound_queue SET scheduled_at = now(), next_retry_at = now() "
                    "WHERE tenant_id = :t AND sent_at IS NULL AND dead_at IS NULL "
                    "AND error_code = :c AND scheduled_at > now()"
                ),
                {"t": tenant_id, "c": OUT_OF_WINDOW},
            )
            return int(result.rowcount)

    async def _suspend(self, tenant_id: int) -> None:
        async with self._engine.begin() as conn:
            await self._scope(conn, tenant_id)
            await conn.execute(
                text("UPDATE tenant SET state = 'suspended', updated_at = now() WHERE id = :i"),
                {"i": tenant_id},
            )

    @staticmethod
    async def _scope(conn: Any, tenant_id: int) -> None:
        """RLS is FORCEd and the app role is not a superuser (§19.1), so every
        write has to say who it is for."""
        await conn.execute(text(f"SET LOCAL app.tenant_id = '{tenant_id}'"))


def _row(found: Any) -> QueuedBubble:
    return QueuedBubble(
        id=int(found.id),
        tenant_id=int(found.tenant_id),
        group_id=UUID(str(found.group_id)),
        seq=int(found.seq),
        kind=str(found.kind),
        payload=dict(found.payload),
        attempts=int(found.attempts),
        sent_at=found.sent_at,
        dead_at=found.dead_at,
        next_retry_at=found.next_retry_at,
        error_code=found.error_code,
        retryable=found.retryable,
        last_error=found.last_error,
    )


def _json(payload: dict[str, Any]) -> str:
    import json

    return json.dumps(payload, ensure_ascii=False)


def _now(offset: int) -> Any:
    from datetime import UTC, datetime, timedelta

    return datetime.now(UTC) + timedelta(seconds=offset)
