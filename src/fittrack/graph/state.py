"""The shared graph state (spec 8.2) and its reducers (spec 8.8).

`GraphState` is the contract every node and subgraph reads and writes, so two
rules keep parallel execution honest (invariant 9):

- A key that more than one branch can write carries a reducer in its
  `Annotated` metadata. Without it, two branches writing the same key in one
  super-step raise `InvalidUpdateError` — a hard failure on the first parallel
  run, never a silently dropped update.
- Every key is classified in `CONCURRENT_KEYS` or `SINGLE_WRITER_KEYS`, the
  latter annotated with the branch allowed to write it. A key that lands in
  neither fails `tests/test_graph_reducers.py` — adding state is a decision,
  not a field.

Two accumulators need more than `operator.add`:

- The four per-batch accumulators (`extracted_sets`, `persisted_set_ids`,
  `outbound`, `errors`) must be emptyable between batches on the same thread.
  The langgraph pin (`>=0.6,<0.8`) offers no overwrite channel, so the runner
  emits a `BatchReset` sentinel instead of a plain empty list — a shape a
  model could produce, and which would then be indistinguishable from "append
  nothing".
- `messages` keeps the semantics of `add_messages` (append, and replace in
  place by message id) but is pruned deterministically to the newest
  `MESSAGE_WINDOW` messages; older ones live in `conversation_digest`
  (spec 8.2: the window cannot grow with the checkpoint).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Annotated, Any, Literal, TypedDict, cast

from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages

Target = Literal["ingestion", "analysis", "recommendation", "admin", "smalltalk"]


class RouteStep(TypedDict):
    """One step the router proposes (spec 9.4). The payload is per-branch."""

    target: Target
    intent: str  # a closed vocabulary per target, never free text
    payload: dict[str, Any]


PlanStage = list[RouteStep]
"""Steps that run in parallel. Stages run in order — the rule is spec 8.8,
applied by `fittrack.graph.staging.stage_plan`, never by the model."""


# --------------------------------------------------------------------------- #
# Accumulator plumbing
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class BatchReset[T]:
    """Sentinel the runner emits to empty an accumulator before a new batch.

    A plain empty list is an update that appends nothing; it must never erase.
    Only an instance of this exact class replaces the accumulated value, and
    it is constructed by the runner in one place, from code — never by a node,
    and never derivable from anything a model wrote.
    """

    items: Sequence[T]


def resettable_add[T](current: list[T], update: Sequence[T] | BatchReset[T]) -> list[T]:
    """Merge an accumulator update: append, unless the update is a batch reset."""
    if isinstance(update, BatchReset):
        return list(update.items)
    return [*current, *update]


MESSAGE_WINDOW = 12


def bounded_add_messages(
    left: Sequence[AnyMessage] | None, right: Sequence[AnyMessage] | None
) -> list[AnyMessage]:
    """`add_messages` with a deterministic cap of the newest messages.

    The window is pruned from the front: the twelve newest messages survive,
    everything older is supposed to have been compressed into
    `conversation_digest` by the SUMMARY tier (spec 8.2). Same-id updates
    still replace in place, exactly as `add_messages` would.
    """
    # The channel is a list of messages by construction; add_messages types
    # itself loosely (strings and dicts are message-like too), so the call
    # site narrows the inputs and the result.
    merged = add_messages(left, right)  # type: ignore[arg-type]
    return cast("list[AnyMessage]", merged)[-MESSAGE_WINDOW:]


# --------------------------------------------------------------------------- #
# The state itself
# --------------------------------------------------------------------------- #


class GraphState(TypedDict):
    """The state every node and subgraph shares (spec 8.2).

    Concurrency per key is not a style choice: a key written by more than one
    branch of a stage carries a reducer, a key written by exactly one carries
    the owner's name in `SINGLE_WRITER_KEYS`, and the classification tests in
    `tests/test_graph_reducers.py` fail if a new key shows up without either.
    """

    # --- input, immutable during a run (the worker provides these) ---
    tenant_id: int
    batch_id: int
    raw_fragments: list[dict[str, Any]]  # [{text, channel, channel_message_id, was_audio}]
    origin_channel: Literal["telegram", "whatsapp"]
    reply_to: tuple[str, str]  # (channel, channel_message_id) of the last message
    destination_identity_id: int  # the identity of the last message, never from reply_to

    # --- context loaded before the graph ---
    profile: dict[str, Any]  # athlete_profile + subscription tier
    active_session: dict[str, Any] | None
    now_local: str  # ISO in the tenant's timezone
    channel_caps: dict[str, Any]  # descriptor of spec 18.1 — read only by voice_agent

    # --- normalization (spec 9.3) ---
    turn: dict[str, Any] | None  # NormalizedTurn: clean text + metadata

    # --- conversation ---
    messages: Annotated[list[AnyMessage], bounded_add_messages]
    conversation_digest: str  # rolling summary of older interactions

    # --- routing ---
    plan: list[PlanStage]
    stage_cursor: int

    # --- subgraph results ---
    # Keys written by branches that can run in parallel carry a reducer.
    # Without it, two branches writing the same key in one super-step raise
    # InvalidUpdateError. See spec 8.8.
    extracted_sets: Annotated[list[dict[str, Any]], resettable_add]
    persisted_set_ids: Annotated[list[int], resettable_add]
    analysis_result: dict[str, Any] | None  # written only by `analysis`
    recommendation: dict[str, Any] | None  # written only by `recommendation`
    query_result: dict[str, Any] | None  # written only by `admin`
    health_flag: dict[str, Any] | None  # written only by guardrail, before the fan-out

    # --- output control ---
    outbound: Annotated[list[dict[str, Any]], resettable_add]  # every branch appends blocks
    errors: Annotated[list[str], resettable_add]
    confidence: float
    pending_clarification: dict[str, Any] | None


# --------------------------------------------------------------------------- #
# The classification
# --------------------------------------------------------------------------- #

CONCURRENT_KEYS: frozenset[str] = frozenset(
    {
        "extracted_sets",  # resettable_add — the ingestion subgraph
        "persisted_set_ids",  # resettable_add — the ingestion subgraph
        "outbound",  # resettable_add — every branch, via semantic blocks
        "errors",  # resettable_add — every branch
        "messages",  # bounded_add_messages — normalizer, voice, summarizer
    }
)

# The one key the guarded packages may only declare, not read (spec 18.1). Its
# name is derived from the field itself so this module spells it nowhere else —
# the architecture test counts the occurrences of the declaration and allows
# exactly one.
_CAPS_KEY = next(name for name in GraphState.__annotations__ if name.endswith("caps"))

# Key -> the branch allowed to write it. "worker input" means the runner
# provides the value before the graph starts and nodes only read it.
SINGLE_WRITER_KEYS: Mapping[str, str] = {
    "tenant_id": "worker input",
    "batch_id": "worker input",
    "raw_fragments": "worker input",
    "origin_channel": "worker input",
    "reply_to": "worker input",
    "destination_identity_id": "worker input",
    "profile": "worker input",
    "active_session": "worker input",
    "now_local": "worker input",
    _CAPS_KEY: "worker input",
    "turn": "normalizer",
    "conversation_digest": "summarizer between batches (spec 8.2)",
    "plan": "router, via stage_plan (spec 9.4 rule 1)",
    "stage_cursor": "join",
    "analysis_result": "analysis subgraph",
    "recommendation": "recommendation subgraph",
    "query_result": "admin subgraph",
    "health_flag": "guardrail, before the fan-out",
    "confidence": "extraction",
    "pending_clarification": "clarification",
}
