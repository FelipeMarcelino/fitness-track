"""Keeping the message window bounded (§8.1).

The failure this prevents is gradual, which is why it needs to be deliberate:
an unbounded window grows a little every turn, every turn costs a little more,
and nothing actually breaks until some conversation stops fitting in a context
at all -- by which point the expensive part has been running for weeks.
"""

from __future__ import annotations

from typing import Any, Final

# §8.1. Everything older is compressed into conversation_digest by the SUMMARY
# tier every 20 interactions.
MAX_MESSAGES: Final = 12


def prune_messages(messages: list[Any]) -> list[Any]:
    """Keeps the most recent MAX_MESSAGES.

    Idempotent, because it runs after every execution: applying it to an
    already-pruned window must not keep shaving the conversation down.
    """
    if len(messages) <= MAX_MESSAGES:
        return messages
    return messages[-MAX_MESSAGES:]
