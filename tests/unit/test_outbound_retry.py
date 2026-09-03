"""Delivery retry and persistence policy for Sprint 02 task S02-T06.

Spec: sections 17.4, 18.2 (sending), and 18.4.
"""

from __future__ import annotations

import base64
import json
import os
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import ClassVar
from uuid import UUID

import pytest

from fittrack.channels.base import (
    ChannelCaps,
    ChannelIdentity,
    ClassifiedError,
    ErrorClass,
    OutboundBlock,
    SendReceipt,
)
from fittrack.services.outbound import (
    BACKOFF_LADDER_SECONDS,
    MAX_RETRIES,
    DeliveryStatus,
    NewOutbound,
    OutboundItem,
    OutboundService,
    RedisRateLimiter,
    retry_delay,
    unsupported_media_block,
)
from fittrack.settings import ChannelKind, get_settings

NOW = datetime(2026, 9, 2, 14, 0, tzinfo=UTC)
IDENTITY = ChannelIdentity(
    identity_id=41,
    tenant_id=7,
    channel="telegram",
    external_id="123456789",
)
PROMPT = Path(__file__).resolve().parents[2] / "config" / "prompts" / "unsupported_media.md"


@pytest.mark.parametrize(
    ("retry_number", "expected"),
    [(1, 2), (2, 8), (3, 32), (4, 120), (5, 480)],
)
def test_backoff_uses_the_spec_ladder_without_jitter(retry_number: int, expected: int) -> None:
    verdict = ClassifiedError(ErrorClass.RETRY_BACKOFF, code="503")
    assert retry_delay(verdict, retry_number, jitter=lambda: 0.0) == timedelta(seconds=expected)


@pytest.mark.parametrize("jitter", [-0.25, 0.25])
def test_backoff_jitter_is_bounded_to_twenty_five_percent(jitter: float) -> None:
    verdict = ClassifiedError(ErrorClass.RETRY_BACKOFF)
    assert retry_delay(verdict, 3, jitter=lambda: jitter) == timedelta(
        seconds=BACKOFF_LADDER_SECONDS[2] * (1 + jitter)
    )


def test_retry_after_uses_the_literal_channel_wait_without_jitter() -> None:
    verdict = ClassifiedError(ErrorClass.RETRY_AFTER, retry_after=17, code="429")
    assert retry_delay(verdict, 2, jitter=lambda: 0.25) == timedelta(seconds=17)


@pytest.mark.parametrize(
    "verdict",
    [
        ClassifiedError(ErrorClass.UNDELIVERABLE, code="403"),
        ClassifiedError(ErrorClass.ACCOUNT, code="401"),
        ClassifiedError(ErrorClass.BUG, code="400"),
        ClassifiedError(ErrorClass.DEFER_WINDOW, code="131047"),
    ],
)
def test_non_retryable_classes_have_no_retry_delay(verdict: ClassifiedError) -> None:
    assert retry_delay(verdict, 1, jitter=lambda: 0.0) is None


@pytest.mark.parametrize("error_class", [ErrorClass.RETRY_BACKOFF, ErrorClass.RETRY_AFTER])
def test_retryable_classes_stop_after_five_retries(error_class: ErrorClass) -> None:
    verdict = ClassifiedError(
        error_class,
        retry_after=3 if error_class is ErrorClass.RETRY_AFTER else None,
    )
    assert retry_delay(verdict, MAX_RETRIES, jitter=lambda: 0.0) is not None
    assert retry_delay(verdict, MAX_RETRIES + 1, jitter=lambda: 0.0) is None


def test_proactive_messages_never_retry_automatically() -> None:
    verdict = ClassifiedError(ErrorClass.RETRY_BACKOFF, code="503")
    assert retry_delay(verdict, 1, proactive=True, jitter=lambda: 0.0) is None


def test_the_fixed_media_degradation_is_a_versioned_prompt() -> None:
    block = unsupported_media_block(PROMPT)
    assert block == OutboundBlock(kind="text", text=PROMPT.read_text(encoding="utf-8").strip())
    assert block.text


def test_the_media_degradation_prompt_honours_the_configured_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configured = tmp_path / "operator-config"
    prompt = configured / "prompts" / "unsupported_media.md"
    prompt.parent.mkdir(parents=True)
    prompt.write_text("Mensagem configurada pelo operador.\n", encoding="utf-8")

    for name in list(os.environ):
        if name.startswith(("FITTRACK_", "DATABASE_", "REDIS_", "QDRANT_", "TELEGRAM_", "WABA_")):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)
    settings_environment = {
        "DATABASE_URL": (
            "postgresql+asyncpg://fittrack_runtime:p@postgres:5432/f?sslmode=verify-full"
        ),
        "REDIS_URL": "rediss://:p@redis:6379/0",
        "QDRANT_URL": "https://qdrant:6333",
        "FITTRACK_CHANNELS": "",
        "FITTRACK_ENCRYPTION_KEYS": json.dumps({"1": base64.b64encode(b"A" * 32).decode()}),
        "FITTRACK_ACTIVE_KEY_VERSION": "1",
        "FITTRACK_IDENTITY_PEPPER": "an-independent-test-pepper-long-enough",
        "FITTRACK_CONFIG_DIR": str(configured),
    }
    for name, value in settings_environment.items():
        monkeypatch.setenv(name, value)
    get_settings.cache_clear()
    try:
        assert unsupported_media_block().text == "Mensagem configurada pelo operador."
    finally:
        get_settings.cache_clear()


@dataclass
class FakeStore:
    queued: list[NewOutbound] = field(default_factory=list)
    retried: list[tuple[int, int, datetime, str | None]] = field(default_factory=list)
    dead: list[tuple[int, UUID, int, int, str]] = field(default_factory=list)
    sent: list[tuple[int, int, int, SendReceipt]] = field(default_factory=list)
    revoked: list[tuple[int, int, datetime]] = field(default_factory=list)

    async def enqueue(self, rows: Sequence[NewOutbound]) -> None:
        self.queued.extend(rows)

    async def mark_sent(
        self, *, item_id: int, tenant_id: int, attempts: int, receipt: SendReceipt
    ) -> None:
        self.sent.append((item_id, tenant_id, attempts, receipt))

    async def revoke_identity(
        self, *, identity_id: int, tenant_id: int, revoked_at: datetime
    ) -> None:
        self.revoked.append((identity_id, tenant_id, revoked_at))

    async def mark_retry(
        self,
        *,
        item_id: int,
        tenant_id: int,
        attempts: int,
        next_retry_at: datetime,
        error_code: str | None,
    ) -> None:
        self.retried.append((item_id, attempts, next_retry_at, error_code))

    async def mark_dead(
        self,
        *,
        item_id: int,
        tenant_id: int,
        group_id: UUID,
        seq: int,
        attempts: int,
        error_code: str,
        dead_at: datetime,
    ) -> None:
        self.dead.append((item_id, group_id, seq, attempts, error_code))


class NoopLimiter:
    def __init__(self) -> None:
        self.identities: list[int] = []

    async def acquire(self, identity_id: int) -> None:
        self.identities.append(identity_id)


class StubChannel:
    kind: ClassVar[ChannelKind] = "telegram"
    caps = ChannelCaps(
        reactions=True,
        reaction_set="restricted",
        buttons=True,
        max_buttons=8,
        text_limit=4096,
        caption_limit=1024,
        typing_indicator=True,
        edit_message=True,
        delete_message=True,
        proactive="free",
        window_hours=None,
        media_upload="inline",
        markup="telegram_html",
        max_bubbles=3,
    )

    def __init__(self, failure: ClassifiedError | None = None) -> None:
        self.failure = failure

    async def send(self, identity: ChannelIdentity, block: OutboundBlock) -> SendReceipt:
        if self.failure is not None:
            raise RuntimeError("redacted channel failure")
        return SendReceipt(
            channel="telegram",
            channel_message_id="99",
            sent_at=NOW,
        )

    def classify_error(self, exc: Exception) -> ClassifiedError:
        assert self.failure is not None
        return self.failure


def service(store: FakeStore, *, jitter: Callable[[], float] = lambda: 0.0) -> OutboundService:
    return OutboundService(
        store=store,
        rate_limiter=NoopLimiter(),
        now=lambda: NOW,
        jitter=jitter,
        group_id=lambda: UUID("11111111-1111-1111-1111-111111111111"),
    )


async def test_every_block_is_queued_with_one_response_group_and_increasing_sequence() -> None:
    store = FakeStore()
    blocks = [OutboundBlock(kind="text", text="first"), OutboundBlock(kind="text", text="second")]
    group_id = await service(store).enqueue_response(
        tenant_id=7,
        identity_id=41,
        channel="telegram",
        blocks=blocks,
    )
    assert group_id == UUID("11111111-1111-1111-1111-111111111111")
    assert [row.group_id for row in store.queued] == [group_id, group_id]
    assert [row.seq for row in store.queued] == [0, 1]


async def test_local_media_is_rejected_before_an_unusable_path_is_queued(tmp_path: Path) -> None:
    media_path = tmp_path / "worker-local-photo.jpg"
    media_path.write_bytes(b"private image")
    store = FakeStore()

    with pytest.raises(ValueError, match="durable shared media storage"):
        await service(store).enqueue_response(
            tenant_id=7,
            identity_id=41,
            channel="telegram",
            blocks=[OutboundBlock(kind="media", media_path=media_path)],
        )

    assert store.queued == []


@pytest.mark.parametrize("media_kind", ["image", "document"])
async def test_each_unsupported_media_kind_gets_a_fixed_grouped_response(
    media_kind: str,
) -> None:
    store = FakeStore()
    group_id = await service(store).enqueue_unsupported_media(
        tenant_id=7,
        identity_id=41,
        channel="telegram",
        media_kind=media_kind,  # type: ignore[arg-type]
        prompt_path=PROMPT,
    )
    assert [(row.group_id, row.seq, row.block.kind) for row in store.queued] == [
        (group_id, 0, "text")
    ]


async def test_every_response_gets_a_new_group_even_when_each_has_one_block() -> None:
    store = FakeStore()
    groups = iter(
        [
            UUID("11111111-1111-1111-1111-111111111111"),
            UUID("22222222-2222-2222-2222-222222222222"),
        ]
    )
    outbound = OutboundService(
        store=store,
        rate_limiter=NoopLimiter(),
        now=lambda: NOW,
        jitter=lambda: 0.0,
        group_id=lambda: next(groups),
    )
    first = await outbound.enqueue_response(
        tenant_id=7,
        identity_id=41,
        channel="telegram",
        blocks=[OutboundBlock(kind="text", text="first")],
    )
    second = await outbound.enqueue_response(
        tenant_id=7,
        identity_id=41,
        channel="telegram",
        blocks=[OutboundBlock(kind="text", text="second")],
    )
    assert first != second


def item(
    *,
    attempts: int = 0,
    proactive: bool = False,
    block: OutboundBlock | None = None,
) -> OutboundItem:
    return OutboundItem(
        id=10,
        tenant_id=7,
        identity_id=41,
        channel="telegram",
        block=block or OutboundBlock(kind="text", text="safe response"),
        group_id=UUID("22222222-2222-2222-2222-222222222222"),
        seq=0,
        attempts=attempts,
        proactive=proactive,
    )


async def test_retry_after_is_persisted_as_the_literal_next_retry_time() -> None:
    store = FakeStore()
    result = await service(store).deliver(
        item=item(),
        identity=IDENTITY,
        channel=StubChannel(ClassifiedError(ErrorClass.RETRY_AFTER, retry_after=17, code="429")),
    )
    assert result.status is DeliveryStatus.RETRY
    assert store.retried == [(10, 1, NOW + timedelta(seconds=17), "429")]


@pytest.mark.parametrize(
    ("accumulated_attempts", "expected_status"),
    [(4, DeliveryStatus.RETRY), (5, DeliveryStatus.DEAD)],
)
async def test_deliver_enforces_the_retry_cap_from_accumulated_attempts(
    accumulated_attempts: int,
    expected_status: DeliveryStatus,
) -> None:
    store = FakeStore()
    result = await service(store).deliver(
        item=item(attempts=accumulated_attempts),
        identity=IDENTITY,
        channel=StubChannel(ClassifiedError(ErrorClass.RETRY_BACKOFF, code="503")),
    )

    assert result.status is expected_status
    assert result.attempts == accumulated_attempts + 1
    assert bool(store.retried) is (expected_status is DeliveryStatus.RETRY)
    assert bool(store.dead) is (expected_status is DeliveryStatus.DEAD)


async def test_deliver_acquires_the_identity_rate_limit_before_sending() -> None:
    events: list[tuple[str, int]] = []

    class OrderedLimiter:
        async def acquire(self, identity_id: int) -> None:
            events.append(("acquire", identity_id))

    class OrderedChannel(StubChannel):
        async def send(self, identity: ChannelIdentity, block: OutboundBlock) -> SendReceipt:
            events.append(("send", identity.identity_id))
            return await super().send(identity, block)

    outbound = OutboundService(
        store=FakeStore(),
        rate_limiter=OrderedLimiter(),
        now=lambda: NOW,
        jitter=lambda: 0.0,
    )
    await outbound.deliver(item=item(), identity=IDENTITY, channel=OrderedChannel())

    assert events == [("acquire", 41), ("send", 41)]


async def test_success_counts_the_send_attempt() -> None:
    store = FakeStore()
    result = await service(store).deliver(
        item=item(attempts=2), identity=IDENTITY, channel=StubChannel()
    )

    assert result.status is DeliveryStatus.SENT
    assert result.attempts == 3
    assert store.sent[0][2] == 3


async def test_undeliverable_failure_revokes_the_target_identity() -> None:
    store = FakeStore()
    await service(store).deliver(
        item=item(),
        identity=IDENTITY,
        channel=StubChannel(ClassifiedError(ErrorClass.UNDELIVERABLE, code="403")),
    )

    assert store.revoked == [(41, 7, NOW)]


@pytest.mark.parametrize(
    ("failure", "expected_status"),
    [
        (None, DeliveryStatus.SENT),
        (ClassifiedError(ErrorClass.ACCOUNT, code="401"), DeliveryStatus.DEAD),
    ],
)
async def test_terminal_delivery_removes_worker_local_media(
    tmp_path: Path,
    failure: ClassifiedError | None,
    expected_status: DeliveryStatus,
) -> None:
    media_path = tmp_path / "temporary-report.pdf"
    media_path.write_bytes(b"private report")

    result = await service(FakeStore()).deliver(
        item=item(block=OutboundBlock(kind="media", media_path=media_path)),
        identity=IDENTITY,
        channel=StubChannel(failure),
    )

    assert result.status is expected_status
    assert not media_path.exists()


@pytest.mark.parametrize(
    ("verdict", "expected_code"),
    [
        (ClassifiedError(ErrorClass.UNDELIVERABLE, code="403"), "403"),
        (ClassifiedError(ErrorClass.ACCOUNT, code="401"), "401"),
        (ClassifiedError(ErrorClass.BUG), "bug"),
    ],
)
async def test_non_retryable_failures_are_persisted_dead_with_an_error_code(
    verdict: ClassifiedError, expected_code: str
) -> None:
    store = FakeStore()
    result = await service(store).deliver(
        item=item(), identity=IDENTITY, channel=StubChannel(verdict)
    )
    assert result.status is DeliveryStatus.DEAD
    assert store.dead == [(10, UUID("22222222-2222-2222-2222-222222222222"), 0, 1, expected_code)]
    assert store.retried == []


async def test_a_proactive_transient_failure_is_persisted_dead_instead_of_retried() -> None:
    store = FakeStore()
    result = await service(store).deliver(
        item=item(proactive=True),
        identity=IDENTITY,
        channel=StubChannel(ClassifiedError(ErrorClass.RETRY_BACKOFF, code="503")),
    )
    assert result.status is DeliveryStatus.DEAD
    assert store.retried == []
    assert store.dead[0][-1] == "503"


class FakeRateLimitRedis:
    """One Redis instance observed by multiple worker-local limiter objects."""

    def __init__(self, waits_ms: list[int]) -> None:
        self.waits_ms = waits_ms
        self.calls: list[tuple[str, int, tuple[str | int, ...]]] = []

    async def eval(self, script: str, numkeys: int, *keys_and_args: str | int) -> int:
        self.calls.append((script, numkeys, keys_and_args))
        return self.waits_ms.pop(0)


async def test_rate_limit_is_shared_through_redis_across_worker_instances() -> None:
    redis = FakeRateLimitRedis([0, 25, 0])
    slept: list[float] = []

    async def sleep(delay: float) -> None:
        slept.append(delay)

    first_worker = RedisRateLimiter(redis, sleep=sleep)
    second_worker = RedisRateLimiter(redis, sleep=sleep)
    await first_worker.acquire(41)
    await second_worker.acquire(42)

    assert slept == [0.025]
    assert len(redis.calls) == 3
    assert all(call[1] == 2 for call in redis.calls)
    assert {call[2][0] for call in redis.calls} == {"outbound:rate:global"}
    assert {call[2][1] for call in redis.calls} == {
        "outbound:rate:identity:41",
        "outbound:rate:identity:42",
    }


def test_rate_limiter_contract_is_global_thirty_per_second_and_one_second_per_chat() -> None:
    assert RedisRateLimiter.GLOBAL_LIMIT == 30
    assert RedisRateLimiter.GLOBAL_WINDOW_MS == 1_000
    assert RedisRateLimiter.CHAT_INTERVAL_MS >= 1_000
    assert MAX_RETRIES == len(BACKOFF_LADDER_SECONDS) == 5
