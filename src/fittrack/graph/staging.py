"""Deterministic staging of the routing plan (spec 8.8, 9.4 rules 1 and 4).

The router proposes steps; this module is the single authority over stages.
Whatever order the model suggests is ignored: `ingestion` writes to the
database and every other target reads from it, so a shared stage could serve
a reader a half-committed write (§8.8). The grouping is a rule, not a
judgment — letting the model decide it would reintroduce that race
intermittently (§9.4 rule 1).

The cap of §9.4 rule 4 bounds each stage at `MAX_STEPS_PER_STAGE` steps. A
stage larger than that is the symptom of a broken prompt, and the steps past
the cap are dropped with an entry in `errors` — never silently re-routed to
some invented destination.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from fittrack.graph.state import PlanStage, RouteStep

MAX_STEPS_PER_STAGE = 4


@dataclass(frozen=True)
class StagedPlan:
    """What `stage_plan` produced: the stages plus every step the cap dropped.

    The truncated steps come back as `errors` because the caller — the router
    node — is the writer of the `errors` accumulator; a pure function cannot
    append to state it does not have (sprint 03 T01, plan item 3).
    """

    stages: list[PlanStage]
    errors: list[str]


def stage_plan(steps: Sequence[RouteStep]) -> StagedPlan:
    """Group the router's proposed steps into parallel stages (spec 8.8).

    Three cases, verbatim from the spec: with `ingestion` proposed, it runs
    alone in stage 1 and the rest is stage 2 in parallel; without it, a single
    stage carries everything. An empty plan stages to nothing — it is valid
    (§9.4 rule 2). The order inside each stage is the order the model
    proposed, but the stages themselves are decided here and only here.
    """
    writers = [step for step in steps if step["target"] == "ingestion"]
    readers = [step for step in steps if step["target"] != "ingestion"]

    errors: list[str] = []
    stages: list[PlanStage] = []

    if writers:
        stages.append(_capped(writers, errors))
    if readers:
        stages.append(_capped(readers, errors))

    return StagedPlan(stages=stages, errors=errors)


def _capped(stage: list[RouteStep], errors: list[str]) -> PlanStage:
    """Truncate a stage to the cap, recording each dropped step in `errors`."""
    kept = stage[:MAX_STEPS_PER_STAGE]
    for step in stage[MAX_STEPS_PER_STAGE:]:
        errors.append(
            f"router: stage cap of {MAX_STEPS_PER_STAGE} steps dropped "
            f"the step toward '{step['target']}'"
        )
    return kept
