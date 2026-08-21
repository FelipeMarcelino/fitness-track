"""The shared graph state (§8.1).

Two rules govern this file, and both fail in production rather than in a type
check.

**Any key two parallel branches might write needs a reducer.** Without one, LangGraph raises InvalidUpdateError when both branches
land in the same super-step, and the user gets an error instead of a partial
answer. "fiz supino 80x8, compara com semana passada" is exactly that case --
the logger and the analyst both want to add a bubble to `outbound` (§8.7).

**Any accumulating key also needs a way to start over.** These channels are
checkpointed, so an appending reducer keeps its contents between invocations
and turn 2 inherits turn 1's bubbles. That is why they use `accumulate` rather
than `operator.add`, and why the first node of every run resets them.

Training context deliberately does not live here. It comes from Postgres
through tools, every time. State that caches it would go stale between turns
and there would be no way to tell which of the two was right.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal, TypedDict

from langgraph.graph.message import add_messages

from fittrack.graph.reducers import accumulate


class RouteStep(TypedDict):
    target: Literal["ingestion", "insight", "coach", "admin", "smalltalk"]
    intent: str
    payload: dict[str, Any]


# A STAGE is a set of steps that run in PARALLEL. Stages run in order. The
# supervisor decides the grouping; the rule is in §8.7.
PlanStage = list[RouteStep]


class GraphState(TypedDict, total=False):
    # --- input ---
    tenant_id: int
    bsuid: str
    batch_id: int
    input_text: str
    message_ids: list[str]
    has_audio: bool

    # --- context loaded before the graph runs ---
    profile: dict[str, Any]
    active_session: dict[str, Any] | None
    now_local: str

    # --- conversation ---
    messages: Annotated[list[Any], add_messages]
    conversation_digest: str

    # --- routing ---
    plan: list[PlanStage]
    stage_cursor: int

    # --- subgraph results ---
    # Fields written by branches that can run in parallel NEED a reducer.
    extracted_sets: Annotated[list[dict[str, Any]], accumulate]
    persisted_set_ids: Annotated[list[int], accumulate]
    # These three are written by one branch each, so they cannot collide.
    analysis_result: dict[str, Any] | None
    recommendation: dict[str, Any] | None
    query_result: dict[str, Any] | None
    # Written only by the guardrail, before the fan-out.
    health_flag: dict[str, Any] | None

    # --- output control ---
    outbound: Annotated[list[dict[str, Any]], accumulate]
    # Any branch can fail, and two failing at once is the case worth surviving:
    # an errors key without a reducer turns two failures into an unrelated
    # third one. §8.1 used to declare this field twice, the second
    # time without the reducer -- in Python the second wins, which silently
    # removed it. Corrected in the spec; kept here as a note because the
    # mistake is invisible on inspection.
    errors: Annotated[list[str], accumulate]
    ack_mode: Literal["reaction", "text", "silent"]
    confidence: float
    pending_clarification: dict[str, Any] | None

    # --- diagnostics ---
    # Which nodes ran. Not part of the product; it is how a topology change
    # becomes visible to a test instead of to a user.
    trace: Annotated[list[str], accumulate]


def initial_state(
    *,
    tenant_id: int,
    bsuid: str,
    batch_id: int,
    input_text: str,
    message_ids: list[str],
    has_audio: bool = False,
) -> GraphState:
    """Builds the state for one batch.

    The accumulating keys start empty rather than absent: a reducer that gets
    None instead of a list fails at the first fan-out, and the first fan-out is
    in production.
    """
    return GraphState(
        tenant_id=tenant_id,
        bsuid=bsuid,
        batch_id=batch_id,
        input_text=input_text,
        message_ids=message_ids,
        has_audio=has_audio,
        profile={},
        active_session=None,
        now_local="",
        messages=[],
        conversation_digest="",
        plan=[],
        stage_cursor=0,
        extracted_sets=[],
        persisted_set_ids=[],
        analysis_result=None,
        recommendation=None,
        query_result=None,
        health_flag=None,
        outbound=[],
        errors=[],
        ack_mode="reaction",
        confidence=1.0,
        pending_clarification=None,
        trace=[],
    )
