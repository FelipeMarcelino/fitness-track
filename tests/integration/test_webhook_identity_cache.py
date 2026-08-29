"""Identity-cache contract for the Telegram ingress (Sprint 02, S02-T03)."""

from __future__ import annotations

from fittrack.services.webhook import CachedIdentityResolver, IngressIdentity
from fittrack.settings import ChannelKind


class MemoryCache:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.writes: list[tuple[str, str, int]] = []

    async def get(self, key: str) -> str | None:
        return self.values.get(key)

    async def set(self, key: str, value: str, *, ex: int) -> None:
        self.values[key] = value
        self.writes.append((key, value, ex))


class RecordingResolver:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def resolve_or_create(self, *, channel: ChannelKind, external_id: str) -> IngressIdentity:
        self.calls.append((channel, external_id))
        return IngressIdentity(tenant_id=19, identity_id=29, external_id_hash=b"ignored")


def hash_identity(channel: ChannelKind, external_id: str) -> bytes:
    assert channel == "telegram"
    assert external_id == "private-chat-id"
    return b"cache-safe-hash"


async def test_identity_cache_uses_only_the_hash_and_avoids_a_second_lookup() -> None:
    cache = MemoryCache()
    delegate = RecordingResolver()
    resolver = CachedIdentityResolver(
        cache=cache,
        delegate=delegate,
        hash_identity=hash_identity,
    )

    first = await resolver.resolve_or_create(channel="telegram", external_id="private-chat-id")
    second = await resolver.resolve_or_create(channel="telegram", external_id="private-chat-id")

    assert first == second == IngressIdentity(19, 29, b"cache-safe-hash")
    assert delegate.calls == [("telegram", "private-chat-id")]
    assert cache.writes == [
        (
            "identity:telegram:63616368652d736166652d68617368",
            '{"identity_id":29,"tenant_id":19}',
            300,
        )
    ]
    assert "private-chat-id" not in repr(cache.values)
