"""The graph skeleton: state, reducers, checkpointing and pruning (§8.1).

Nothing here calls a model. What is being tested is the plumbing that every
agent will depend on, and the three ways it silently breaks: state that does
not survive a restart, parallel branches that cannot both write, and a message
window that grows without bound.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from fittrack.graph.prune import MAX_MESSAGES, prune_messages
from fittrack.graph.state import GraphState, initial_state

pytestmark = pytest.mark.integration


# --- state and reducers ----------------------------------------------------


def test_every_field_a_parallel_branch_writes_has_a_reducer() -> None:
    """§8.7's whole point.

    Two branches writing the same key in one super-step raise
    InvalidUpdateError unless the key has a reducer. The failure surfaces at
    runtime, under fan-out, in production -- exactly where it is most expensive
    to discover -- so it is pinned here instead.
    """
    from typing import get_type_hints

    hints = get_type_hints(GraphState, include_extras=True)
    concurrent = ["extracted_sets", "persisted_set_ids", "outbound", "errors", "messages"]

    for field_name in concurrent:
        annotation = hints[field_name]
        assert hasattr(annotation, "__metadata__"), (
            f"{field_name} is written by parallel branches and has no reducer; "
            f"a fan-out will raise InvalidUpdateError"
        )


def test_the_initial_state_carries_what_the_batch_knows() -> None:
    state = initial_state(
        tenant_id=1, bsuid="BSUID-1", batch_id=7, input_text="oi", message_ids=["m1"]
    )

    assert state["tenant_id"] == 1
    assert state["batch_id"] == 7
    assert state["outbound"] == []
    assert state["errors"] == []


# --- pruning ---------------------------------------------------------------


def test_pruning_keeps_the_last_twelve_messages() -> None:
    """§8.1. Unbounded, the window grows until every turn pays for the whole
    conversation, and eventually until it does not fit at all."""
    messages = [{"role": "user", "content": f"m{i}"} for i in range(30)]

    kept = prune_messages(messages)

    assert len(kept) == MAX_MESSAGES
    assert kept[-1]["content"] == "m29"
    assert kept[0]["content"] == f"m{30 - MAX_MESSAGES}"


def test_pruning_a_short_history_changes_nothing() -> None:
    messages = [{"role": "user", "content": "oi"}]
    assert prune_messages(messages) == messages


def test_pruning_is_idempotent() -> None:
    """It runs after every execution, so applying it twice must not shrink the
    window further."""
    messages = [{"role": "user", "content": f"m{i}"} for i in range(30)]
    once = prune_messages(messages)
    assert prune_messages(once) == once


# --- the graph itself ------------------------------------------------------


async def test_two_branches_writing_outbound_do_not_collide(app_dsn: str) -> None:
    """The test §8.7 asks for by name.

    "fiz supino 80x8, compara com semana passada" runs the logger and the
    analyst in the same super-step, and both want to add a bubble. Without a
    reducer on `outbound` LangGraph raises InvalidUpdateError and the user gets
    nothing at all -- not a partial answer, an error.
    """
    from fittrack.graph.build import build_fanout_probe

    graph = build_fanout_probe()
    result = await graph.ainvoke(
        initial_state(tenant_id=1, bsuid="BSUID-1", batch_id=1, input_text="oi", message_ids=[])
    )

    bodies = sorted(bubble["body"] for bubble in result["outbound"])
    assert bodies == ["from the analyst", "from the logger"]
    assert sorted(result["errors"]) == ["analyst complained", "logger complained"]


async def test_state_survives_between_invocations_on_one_thread(
    checkpointed_graph: Any,
) -> None:
    """A restart mid-conversation must not lose the thread.

    Without a checkpointer the second message starts from nothing: the bot asks
    again for what the user already said, which is the single most obvious way
    for it to look broken.
    """
    graph, thread = checkpointed_graph

    await graph.ainvoke(
        initial_state(
            tenant_id=1, bsuid="BSUID-1", batch_id=1, input_text="oi", message_ids=["m1"]
        ),
        config=thread,
    )
    second = await graph.ainvoke(
        initial_state(
            tenant_id=1, bsuid="BSUID-1", batch_id=2, input_text="tudo bem?", message_ids=["m2"]
        ),
        config=thread,
    )

    # Both turns are in the window, in order.
    contents = [_content(message) for message in second["messages"]]
    assert "oi" in contents
    assert "tudo bem?" in contents
    assert contents.index("oi") < contents.index("tudo bem?")


async def test_a_different_thread_starts_clean(checkpointed_graph: Any) -> None:
    """Threads are per user. One tenant's history leaking into another's is a
    privacy incident, not a bug."""
    graph, thread = checkpointed_graph

    await graph.ainvoke(
        initial_state(
            tenant_id=1, bsuid="BSUID-1", batch_id=1, input_text="segredo", message_ids=["m1"]
        ),
        config=thread,
    )
    other = await graph.ainvoke(
        initial_state(
            tenant_id=2, bsuid="BSUID-2", batch_id=2, input_text="oi", message_ids=["m2"]
        ),
        config={"configurable": {"thread_id": "BSUID-2"}},
    )

    assert "segredo" not in [_content(message) for message in other["messages"]]


async def test_the_window_stays_bounded_across_many_turns(
    checkpointed_graph: Any,
) -> None:
    """The failure this prevents is gradual: the window grows, every turn costs
    more, and nothing breaks until a conversation stops fitting."""
    graph, thread = checkpointed_graph

    for i in range(20):
        result = await graph.ainvoke(
            initial_state(
                tenant_id=1,
                bsuid="BSUID-1",
                batch_id=i,
                input_text=f"mensagem {i}",
                message_ids=[f"m{i}"],
            ),
            config=thread,
        )

    assert len(result["messages"]) <= MAX_MESSAGES


async def test_saying_oi_produces_an_acknowledgement(checkpointed_graph: Any) -> None:
    """The sprint's definition of done: "oi" comes back as a reaction."""
    graph, thread = checkpointed_graph

    result = await graph.ainvoke(
        initial_state(
            tenant_id=1, bsuid="BSUID-1", batch_id=1, input_text="oi", message_ids=["m1"]
        ),
        config=thread,
    )

    assert result["outbound"], "nothing to send back"
    assert result["ack_mode"] in {"reaction", "text"}
    assert any("✅" in str(bubble.get("payload", bubble)) for bubble in result["outbound"])


async def test_an_audio_message_reaches_the_voice_step(checkpointed_graph: Any) -> None:
    """The stub does nothing yet, but the branch has to exist: wiring it later
    means changing the topology under a working system."""
    graph, thread = checkpointed_graph

    result = await graph.ainvoke(
        {
            **initial_state(
                tenant_id=1, bsuid="BSUID-1", batch_id=1, input_text="", message_ids=["m1"]
            ),
            "has_audio": True,
        },
        config=thread,
    )

    assert "voice" in result["trace"]


async def test_concurrent_threads_do_not_interleave(checkpointed_graph: Any) -> None:
    """Different users run in parallel by design (§17.3)."""
    graph, _ = checkpointed_graph

    async def run(bsuid: str, tenant_id: int) -> Any:
        return await graph.ainvoke(
            initial_state(
                tenant_id=tenant_id,
                bsuid=bsuid,
                batch_id=1,
                input_text=f"sou {bsuid}",
                message_ids=["m1"],
            ),
            config={"configurable": {"thread_id": bsuid}},
        )

    first, second = await asyncio.gather(run("BSUID-A", 1), run("BSUID-B", 2))

    assert "sou BSUID-A" in [_content(m) for m in first["messages"]]
    assert "sou BSUID-B" not in [_content(m) for m in first["messages"]]
    assert "sou BSUID-B" in [_content(m) for m in second["messages"]]


def _content(message: Any) -> str:
    return str(getattr(message, "content", None) or message.get("content", ""))


async def test_a_second_turn_does_not_resend_the_first_turns_bubbles(
    checkpointed_graph: Any,
) -> None:
    """The accumulating reducers span turns unless something clears them.

    `outbound` is checkpointed and uses an appending reducer, so turn 2's
    result carries turn 1's bubbles too -- passing outbound=[] in the input
    does not clear the channel. Left alone, every message would re-deliver
    every acknowledgement the user has ever received, growing by one each time.
    """
    graph, thread = checkpointed_graph

    first = await graph.ainvoke(
        initial_state(
            tenant_id=1, bsuid="BSUID-1", batch_id=1, input_text="oi", message_ids=["m1"]
        ),
        config=thread,
    )
    assert len(first["outbound"]) == 1

    second = await graph.ainvoke(
        initial_state(
            tenant_id=1, bsuid="BSUID-1", batch_id=2, input_text="tudo bem?", message_ids=["m2"]
        ),
        config=thread,
    )

    assert len(second["outbound"]) == 1, "the previous turn's bubbles came back"
    assert second["outbound"][0]["payload"]["message_ids"] == ["m2"]

    third = await graph.ainvoke(
        initial_state(
            tenant_id=1, bsuid="BSUID-1", batch_id=3, input_text="beleza", message_ids=["m3"]
        ),
        config=thread,
    )
    assert len(third["outbound"]) == 1


async def test_the_trace_is_per_turn_too(checkpointed_graph: Any) -> None:
    """Same mechanism, and the one that would make the growth obvious in a
    log long before anyone noticed the duplicate messages."""
    graph, thread = checkpointed_graph

    for i in range(3):
        result = await graph.ainvoke(
            initial_state(
                tenant_id=1, bsuid="BSUID-1", batch_id=i, input_text="x", message_ids=[f"m{i}"]
            ),
            config=thread,
        )

    assert result["trace"].count("echo") == 1
