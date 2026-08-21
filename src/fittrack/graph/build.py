"""The minimal graph: load_context -> echo -> voice_stub -> deliver (§8.2).

A walking skeleton. No model is called and nothing is extracted; what this
proves is that a message can enter the graph, produce a reply, and have its
state survive to the next turn. Every agent added later plugs into topology
that already works, rather than arriving alongside the plumbing.
"""

from __future__ import annotations

import logging
from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph

from fittrack.graph.prune import prune_messages
from fittrack.graph.reducers import per_turn_reset
from fittrack.graph.state import GraphState

log = logging.getLogger(__name__)

# What the bot reacts with to acknowledge a message it has taken in (§7.3).
ACK_EMOJI = "✅"


async def begin_turn(state: GraphState) -> dict[str, Any]:
    """Clears what last turn accumulated, and nothing else.

    A node of its own rather than part of load_context, because LangGraph
    applies one update per key per node: a node that returned both the reset
    and its own trace entry would have the entry overwrite the reset, and the
    channel would keep growing.

    The reset cannot live in the input either. These channels are checkpointed
    and their reducer appends, so passing an empty list appends an empty list
    to what is already stored. Without this, every message re-delivers every
    acknowledgement the user has ever received.
    """
    return per_turn_reset()


async def load_context(state: GraphState) -> dict[str, Any]:
    """Where the profile, the open session and the local clock will come from.

    Empty for now, but the node exists so that adding them is a change inside
    one node rather than a change to the topology of a running graph.
    """
    return {"trace": ["load_context"]}


async def echo(state: GraphState) -> dict[str, Any]:
    """Acknowledges the burst.

    A reaction rather than a text bubble: §7.3 wants the cheapest possible
    signal that the message landed, and a chat where every message draws a
    written reply is exhausting to use.
    """
    text = state.get("input_text", "")
    return {
        "messages": [{"role": "user", "content": text}],
        "outbound": [
            {
                "kind": "reaction",
                "payload": {"emoji": ACK_EMOJI, "message_ids": state.get("message_ids", [])},
            }
        ],
        "ack_mode": "reaction",
        "trace": ["echo"],
    }


async def voice_stub(state: GraphState) -> dict[str, Any]:
    """Placeholder for transcription (§11).

    Reached only when the burst carried audio. It does nothing yet and says so
    in the trace: a branch that exists and is empty is testable, whereas a
    branch added later changes the graph under a system already in production.
    """
    if not state.get("has_audio"):
        return {"trace": ["voice:skipped"]}
    log.info(
        "audio received for batch %s; transcription is not wired up yet", state.get("batch_id")
    )
    return {"trace": ["voice"]}


async def deliver(state: GraphState) -> dict[str, Any]:
    """Last node. Prunes the window and hands the bubbles over.

    Pruning here rather than on the way in, because what has to stay bounded is
    what gets *stored*: the checkpoint is written after this node, so a window
    trimmed earlier would grow right back with this turn's messages.
    """
    pruned = prune_messages(list(state.get("messages", [])))
    return {
        # A delta, not the window: add_messages appends, so returning the
        # shorter list would leave the long one in place. What goes back is a
        # RemoveMessage per dropped message.
        "messages": _replace_window(state, pruned),
        "trace": ["deliver"],
    }


def _replace_window(state: GraphState, pruned: list[Any]) -> list[Any]:
    """Returns the removals that shrink the window to `pruned`.

    add_messages appends and merges by id, so it has no notion of "here is the
    new list". The only way to drop a message is to send a RemoveMessage for
    it; the survivors are left untouched rather than re-sent.
    """
    from langchain_core.messages import RemoveMessage

    current = list(state.get("messages", []))
    keep = {id(message) for message in pruned}
    removals = [
        RemoveMessage(id=message.id)
        for message in current
        if id(message) not in keep and getattr(message, "id", None) is not None
    ]
    return removals


def build_graph(checkpointer: BaseCheckpointSaver[Any] | None = None) -> Any:
    """Wires the skeleton.

    voice_stub sits between echo and deliver rather than on a conditional edge:
    at this size a branch would be topology for its own sake, and the node
    already knows how to do nothing.
    """
    graph = StateGraph(GraphState)
    graph.add_node("begin_turn", begin_turn)
    graph.add_node("load_context", load_context)
    graph.add_node("echo", echo)
    graph.add_node("voice_stub", voice_stub)
    graph.add_node("deliver", deliver)

    graph.add_edge(START, "begin_turn")
    graph.add_edge("begin_turn", "load_context")
    graph.add_edge("load_context", "echo")
    graph.add_edge("echo", "voice_stub")
    graph.add_edge("voice_stub", "deliver")
    graph.add_edge("deliver", END)

    return graph.compile(checkpointer=checkpointer)


def build_fanout_probe() -> Any:
    """A graph whose only job is to run two nodes in one super-step.

    It exists because the reducers in §8.1 cannot be verified by reading the
    type: LangGraph only raises InvalidUpdateError when two branches actually
    write the same key at the same time. This reproduces that, so a reducer
    removed by accident fails here instead of on "fiz supino 80x8, compara com
    semana passada" in production.
    """

    async def fan_out(state: GraphState) -> dict[str, Any]:
        return {"trace": ["fan_out"]}

    async def logger_branch(state: GraphState) -> dict[str, Any]:
        return {
            "outbound": [{"body": "from the logger"}],
            "errors": ["logger complained"],
            "extracted_sets": [{"exercise": "supino"}],
        }

    async def analyst_branch(state: GraphState) -> dict[str, Any]:
        return {
            "outbound": [{"body": "from the analyst"}],
            "errors": ["analyst complained"],
            "extracted_sets": [{"exercise": "comparison"}],
        }

    graph = StateGraph(GraphState)
    graph.add_node("fan_out", fan_out)
    graph.add_node("logger", logger_branch)
    graph.add_node("analyst", analyst_branch)

    graph.add_edge(START, "fan_out")
    # Both edges from one node: LangGraph runs them in the same super-step.
    graph.add_edge("fan_out", "logger")
    graph.add_edge("fan_out", "analyst")
    graph.add_edge("logger", END)
    graph.add_edge("analyst", END)

    return graph.compile()
