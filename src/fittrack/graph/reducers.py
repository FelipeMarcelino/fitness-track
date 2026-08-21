"""Reducers for the fields that accumulate within a turn but not across turns.

`operator.add` is the obvious choice for a key two parallel branches write, and
it is a trap once the state is checkpointed: the channel keeps its contents
between invocations, so turn 2 sees turn 1's bubbles as well as its own.
Passing an empty list in the input does not help -- the reducer appends that
empty list to what is already there.

The result is a bug that grows: every message re-delivers every acknowledgement
the user has ever received, one more each time, and nothing looks wrong until
someone counts.

So the reducer understands one extra value. Handing it RESET clears the
channel, and the first node of every run does exactly that.
"""

from __future__ import annotations

from typing import Any, Final

# A string rather than a sentinel object, because node outputs are written to
# the checkpoint through msgpack: a custom class raises "Type is not msgpack
# serializable" the first time a run is persisted. Namespaced so it cannot
# collide with a value a node would legitimately produce -- and only ever
# compared against `right` itself, never against a list's contents, so an
# errors list that happened to contain this text would still append normally.
RESET: Final = "__fittrack_reset__"


def accumulate[T](left: list[T] | None, right: list[T] | str | None) -> list[T]:
    """Appends, unless asked to start over.

    Keeps the parallel-write behaviour §8.7 needs -- two branches writing in
    one super-step both land -- while giving the run a way to begin empty.

    A `None` update leaves the channel alone rather than clearing it. Clearing
    would mean a node that forgets to return a key wipes what its sibling just
    wrote, which is a much harder failure to see than one extra bubble.
    """
    if right == RESET:
        return []
    if right is None:
        return list(left or [])
    if isinstance(right, str):  # pragma: no cover - defensive
        raise TypeError(f"accumulate got the string {right!r}; expected a list or RESET")
    return list(left or []) + list(right)


def per_turn_reset() -> dict[str, Any]:
    """What the first node returns to clear last turn's accumulations.

    `messages` is deliberately absent: the conversation window is the one thing
    that is *supposed* to span turns.
    """
    return {
        "outbound": RESET,
        "errors": RESET,
        "extracted_sets": RESET,
        "persisted_set_ids": RESET,
        "trace": RESET,
    }
