"""Reducers of the shared graph state (spec 8.8, invariant 9).

Every key that more than one parallel branch can write carries a reducer.
Without it, two branches writing the same key in one super-step raise
`InvalidUpdateError` — a hard failure on the first parallel execution, never
a silent drop. One reducer per key turns a whole class of intermittent bugs
into a decision that had to be made up front (§8.2).

Three things are pinned here:

1. A synthetic stage with four `Send` branches writing every concurrent key
   merges instead of exploding, and keeps the merge when the branches write
   in a different order.
2. A plain empty update appends nothing and erases nothing; only the
   `BatchReset` sentinel the runner emits before a new batch replaces the
   accumulated value — and never more than once.
3. Every key of `GraphState` is classified in `CONCURRENT_KEYS` or
   `SINGLE_WRITER_KEYS`. A key that lands in neither fails here, which is
   what stops a state field from being added without a decision.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any, cast, get_args, get_origin, get_type_hints

from langchain_core.messages import HumanMessage
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

from fittrack.graph.state import (
    CONCURRENT_KEYS,
    MESSAGE_WINDOW,
    SINGLE_WRITER_KEYS,
    BatchReset,
    GraphState,
    bounded_add_messages,
    resettable_add,
)

if TYPE_CHECKING:
    from langchain_core.messages import AnyMessage
    from langchain_core.runnables import RunnableConfig
    from langgraph.graph.state import CompiledStateGraph

BRANCHES = 4


# --------------------------------------------------------------------------- #
# Synthetic graphs: one node per branch, each writing every concurrent key
# --------------------------------------------------------------------------- #


def _initial_state(**fields: Any) -> GraphState:
    """A partial initial state; langgraph applies it through the reducers."""
    return cast("GraphState", fields)


def _branch_payload(index: int) -> dict[str, Any]:
    """One branch's update: one item into each concurrent key."""
    return {
        "extracted_sets": [{"branch": index}],
        "persisted_set_ids": [index],
        "outbound": [{"kind": "ack", "branch": index}],
        "errors": [f"noted by branch {index}"],
        "messages": [HumanMessage(content=f"branch {index}")],
    }


def _branch_0(state: GraphState) -> dict[str, Any]:
    return _branch_payload(0)


def _branch_1(state: GraphState) -> dict[str, Any]:
    return _branch_payload(1)


def _branch_2(state: GraphState) -> dict[str, Any]:
    return _branch_payload(2)


def _branch_3(state: GraphState) -> dict[str, Any]:
    return _branch_payload(3)


_BRANCH_NODES = {
    "branch_0": _branch_0,
    "branch_1": _branch_1,
    "branch_2": _branch_2,
    "branch_3": _branch_3,
}


def _dispatch(state: GraphState) -> list[Send]:
    return [Send(name, state) for name in _BRANCH_NODES]


def _noop(state: GraphState) -> dict[str, Any]:
    return {}


def _accumulator_node(state: GraphState) -> dict[str, Any]:
    """A single writer that appends one item to each accumulator per batch."""
    return {
        "extracted_sets": [{"batch": state["batch_id"]}],
        "persisted_set_ids": [state["batch_id"]],
        "outbound": [{"kind": "ack", "batch": state["batch_id"]}],
        "errors": [],
        "messages": [HumanMessage(content=f"turn of batch {state['batch_id']}")],
    }


def _fan_out_graph() -> CompiledStateGraph[GraphState]:
    builder: StateGraph[GraphState] = StateGraph(GraphState)
    builder.add_node("dispatch", _noop)
    for name, node in _BRANCH_NODES.items():
        builder.add_node(name, node)
        builder.add_edge(name, END)
    builder.add_conditional_edges("dispatch", _dispatch, [*_BRANCH_NODES])
    builder.add_edge(START, "dispatch")
    return builder.compile()


def _accumulator_graph(checkpointer: MemorySaver | None = None) -> CompiledStateGraph[GraphState]:
    builder: StateGraph[GraphState] = StateGraph(GraphState)
    builder.add_node("run", _accumulator_node)
    builder.add_edge(START, "run")
    builder.add_edge("run", END)
    return builder.compile(checkpointer=checkpointer)


# --------------------------------------------------------------------------- #
# The reducer functions themselves
# --------------------------------------------------------------------------- #


def test_resettable_add_concatenates_normal_updates() -> None:
    """The default is operator.add: nodes accumulate, nothing is lost."""
    assert resettable_add(["a", "b"], ["c"]) == ["a", "b", "c"]


def test_a_plain_empty_update_never_resets_an_accumulator() -> None:
    """A list the language allows anywhere is not a reset instruction."""
    assert resettable_add(["kept"], []) == ["kept"]


def test_only_the_batch_reset_sentinel_replaces_the_accumulated_value() -> None:
    """The runner's sentinel is the single way an accumulator is emptied."""
    assert resettable_add(["old"], BatchReset([])) == []
    assert resettable_add(["old"], BatchReset(["fresh"])) == ["fresh"]


def test_bounded_add_messages_keeps_only_the_last_window() -> None:
    """Sixteen messages in, the twelve newest out — deterministic pruning."""
    first = [HumanMessage(content=str(number)) for number in range(10)]
    second = [HumanMessage(content=str(number)) for number in range(10, 16)]

    result = bounded_add_messages(first, second)

    assert len(result) == MESSAGE_WINDOW == 12
    assert [message.content for message in result] == [str(number) for number in range(4, 16)]


def test_bounded_add_messages_replaces_by_id_like_add_messages() -> None:
    """The cap must not change `add_messages` semantics: same id updates in place."""
    result = bounded_add_messages(
        [HumanMessage(content="v1", id="same")],
        [HumanMessage(content="v2", id="same")],
    )
    assert len(result) == 1
    assert str(result[0].content) == "v2"


def test_bounded_add_messages_survives_an_empty_merge() -> None:
    assert bounded_add_messages([], []) == []


# --------------------------------------------------------------------------- #
# The rule, against a real compiled graph
# --------------------------------------------------------------------------- #


def test_a_parallel_stage_merges_every_concurrent_key() -> None:
    """Four branches, one super-step, every concurrent key merged — no InvalidUpdateError."""
    result = _fan_out_graph().invoke(_initial_state(batch_id=1))

    assert len(result["outbound"]) == BRANCHES
    assert len(result["errors"]) == BRANCHES
    assert len(result["extracted_sets"]) == BRANCHES
    assert len(result["persisted_set_ids"]) == BRANCHES
    assert len(result["messages"]) == BRANCHES
    assert sorted(block["branch"] for block in result["outbound"]) == [0, 1, 2, 3]


def test_two_batches_on_one_thread_do_not_share_the_accumulators() -> None:
    """A new batch resets the four accumulators exactly once; nothing else leaks."""
    graph = _accumulator_graph(checkpointer=MemorySaver())
    config: RunnableConfig = {"configurable": {"thread_id": "tenant:7"}}

    first = graph.invoke(
        _initial_state(
            batch_id=1,
            conversation_digest="digest one",
            messages=[HumanMessage(content="first batch input")],
        ),
        config,
    )
    assert len(first["outbound"]) == 1
    assert first["conversation_digest"] == "digest one"

    second = graph.invoke(
        _initial_state(
            batch_id=2,
            extracted_sets=BatchReset([]),
            persisted_set_ids=BatchReset([]),
            outbound=BatchReset([]),
            errors=BatchReset([]),
        ),
        config,
    )

    assert second["outbound"] == [{"kind": "ack", "batch": 2}]
    assert second["extracted_sets"] == [{"batch": 2}]
    assert second["persisted_set_ids"] == [2]
    assert second["errors"] == []
    # Single-writer keys are not reset: the digest laid down by batch 1 crosses.
    assert second["conversation_digest"] == "digest one"
    # messages cross the boundary too — they are never reset, only capped.
    contents = [message.content for message in second["messages"]]
    assert contents == [
        "first batch input",
        "turn of batch 1",
        "turn of batch 2",
    ]


def test_a_retry_of_the_same_batch_keeps_the_accumulators() -> None:
    """No new `BatchReset`, no reset: the retry of a batch accumulates on top."""
    graph = _accumulator_graph(checkpointer=MemorySaver())
    config: RunnableConfig = {"configurable": {"thread_id": "tenant:7"}}

    graph.invoke(_initial_state(batch_id=3), config)
    retried = graph.invoke(_initial_state(batch_id=3), config)

    assert len(retried["outbound"]) == 2
    assert retried["extracted_sets"] == [{"batch": 3}, {"batch": 3}]
    assert len(retried["messages"]) == 2


# --------------------------------------------------------------------------- #
# The classification itself
# --------------------------------------------------------------------------- #


def test_every_state_key_is_classified() -> None:
    """A key without a classification is a key without a decision — fail hard."""
    keys = set(GraphState.__annotations__)
    assert keys == CONCURRENT_KEYS | set(SINGLE_WRITER_KEYS)
    assert not CONCURRENT_KEYS & set(SINGLE_WRITER_KEYS)


def test_every_concurrent_key_actually_carries_a_reducer() -> None:
    """The sets and the schema must not drift apart in either direction."""
    hints = get_type_hints(GraphState, include_extras=True)
    for key in CONCURRENT_KEYS:
        assert get_origin(hints[key]) is Annotated, f"{key} claims concurrency but has no reducer"
        assert len(get_args(hints[key])) > 1, f"{key} is Annotated without a reducer"

    for key, owner in SINGLE_WRITER_KEYS.items():
        assert get_origin(hints[key]) is not Annotated, (
            f"{key} is classified as written only by {owner}, yet carries a reducer"
        )


def test_message_contents_of_the_window_are_the_ones_written_last() -> None:
    """Guard the window with graph traffic, not only with the bare reducer."""
    graph = _accumulator_graph()
    result = graph.invoke(_initial_state(batch_id=9))

    assert len(result["messages"]) == 1
    message: AnyMessage = result["messages"][0]
    assert str(message.content) == "turn of batch 9"
