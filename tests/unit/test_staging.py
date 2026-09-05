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

from fittrack.graph.staging import MAX_STEPS_PER_STAGE, _capped, stage_plan
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


def test_a_repeated_target_in_a_stage_is_dropped_and_recorded() -> None:
    """Two analysis steps in one stage would run two subgraphs writing the same
    single-writer result key — the InvalidUpdateError spec 8.2 designed for
    exactly this. Staging keeps the first and records the drop, the same
    treatment spec 9.4 rule 4 gives a plan past the cap."""
    staged = stage_plan(
        [
            _step("analysis", "analyze_progress"),
            _step("analysis", "compare_period"),
        ]
    )

    assert staged.stages == [[_step("analysis", "analyze_progress")]]
    assert staged.errors == [
        "router: a repeated step toward 'analysis' was dropped - single-writer key (spec 8.2)"
    ]


def test_duplicates_in_the_writers_stage_are_dropped_too() -> None:
    """A compound burst ('anota o supino e fecha a sessão') produces two
    ingestion steps; both would run in stage 1 in parallel. The first runs,
    the drop is recorded, and the compound-ingestion capability is a decision
    for the subgraph contract, not for a reducer added here."""
    staged = stage_plan(
        [
            _step("ingestion", "log_workout"),
            _step("ingestion", "close_session"),
        ]
    )

    assert staged.stages == [[_step("ingestion", "log_workout")]]
    assert staged.errors == [
        "router: a repeated step toward 'ingestion' was dropped - single-writer key (spec 8.2)"
    ]


def test_the_cap_clamp_truncates_and_records_errors() -> None:
    """§9.4 rule 4, tested at the clamp itself.

    Through `stage_plan` the clamp is dormant while `Target` has only four
    reader values — the per-target rule bounds every stage first. It guards
    the day `Target` grows past the cap (a new parallelizable target).
    """
    steps = [_step("analysis", f"intent_{number}") for number in range(5)]

    errors: list[str] = []
    kept = _capped(steps, errors)

    assert kept == steps[:MAX_STEPS_PER_STAGE]
    assert errors == [
        f"router: stage cap of {MAX_STEPS_PER_STAGE} steps dropped the step toward 'analysis'"
    ]
