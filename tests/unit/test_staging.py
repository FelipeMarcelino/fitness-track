"""The staging rule (spec 8.8) applied to what the router proposes (§9.4).

The model returns steps; `stage_plan` is the single authority over stages.
These tests pin the three grouping cases of the spec, the cap of §9.4 rule 4,
and the `errors` trail a truncated stage leaves behind.

The order the model suggests is ignored on purpose: `ingestion` writes to the
database and the other targets read from it, so letting the model interleave
them would reintroduce the write/read race intermittently — the worst way a
bug can exist (§8.8, §9.4 rule 1).
"""

from __future__ import annotations

from fittrack.graph.staging import MAX_STEPS_PER_STAGE, stage_plan
from fittrack.graph.state import RouteStep, Target


def _step(target: Target, intent: str = "probe") -> RouteStep:
    return {"target": target, "intent": intent, "payload": {}}


def test_ingestion_runs_alone_in_the_first_stage() -> None:
    """The model put analysis first; the rule does not care (§9.4 rule 1)."""
    staged = stage_plan(
        [
            _step("analysis", "analyze_progress"),
            _step("ingestion", "log_workout"),
            _step("recommendation", "build_plan"),
        ]
    )

    assert staged.stages == [
        [_step("ingestion", "log_workout")],
        [_step("analysis", "analyze_progress"), _step("recommendation", "build_plan")],
    ]
    assert staged.errors == []


def test_without_ingestion_the_plan_is_a_single_stage() -> None:
    steps = [
        _step("analysis", "analyze_volume"),
        _step("admin", "change_persona"),
        _step("smalltalk", "thanks"),
    ]

    staged = stage_plan(steps)

    assert staged.stages == [steps]
    assert staged.errors == []


def test_ingestion_alone_is_a_single_stage_too() -> None:
    staged = stage_plan([_step("ingestion", "close_session")])

    assert staged.stages == [[_step("ingestion", "close_session")]]
    assert staged.errors == []


def test_an_empty_plan_is_valid() -> None:
    """§9.4 rule 2: an empty plan is a valid plan."""
    staged = stage_plan([])

    assert staged.stages == []
    assert staged.errors == []


def test_a_reader_stage_past_the_cap_is_truncated_and_records_errors() -> None:
    """Five reader steps, a cap of four: the fifth is dropped, never re-routed."""
    steps = [
        _step("analysis", "analyze_progress"),
        _step("analysis", "compare_period"),
        _step("analysis", "analyze_volume"),
        _step("analysis", "explain_metric"),
        _step("analysis", "query_history"),
    ]

    staged = stage_plan(steps)

    assert staged.stages == [steps[:MAX_STEPS_PER_STAGE]]
    assert staged.errors == [
        f"router: stage cap of {MAX_STEPS_PER_STAGE} steps dropped the step toward 'analysis'"
    ]


def test_the_cap_binds_the_ingestion_stage_as_well() -> None:
    steps = [_step("ingestion", f"log_{number}") for number in range(5)]

    staged = stage_plan(steps)

    assert staged.stages == [steps[:MAX_STEPS_PER_STAGE]]
    assert len(staged.errors) == 1


def test_duplicate_readers_keep_their_order_inside_the_stage() -> None:
    """Two steps for the same target in one stage are two independent tasks."""
    steps = [
        _step("analysis", "analyze_progress"),
        _step("analysis", "compare_period"),
    ]

    staged = stage_plan(steps)

    assert staged.stages == [steps]
    assert staged.errors == []
