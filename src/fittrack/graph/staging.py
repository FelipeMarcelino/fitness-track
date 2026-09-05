"""Deterministic staging of the routing plan (spec 8.8, 9.4 rules 1 and 4).

The router proposes steps; this module is the single authority over stages.
Whatever order the model suggests is ignored: `ingestion` writes to the
database and every other target reads from it, so a shared stage could serve
a reader a half-committed write (§8.8). The grouping is a rule, not a
judgment — letting the model decide it would reintroduce that race
intermittently (§9.4 rule 1).

Two bounds protect the parallel machinery, and both are enforced here:

- **One step per target per stage.** The reader result keys of spec 8.2
  (`analysis_result`, `recommendation`, `query_result`) are deliberately
  single-writer and reducer-less; two steps toward the same target would run
  two subgraph instances in one super-step and raise `InvalidUpdateError` —
  the hard failure spec 8.2 designed for exactly that. A repeated step is
  dropped with an entry in `errors`, the same treatment spec 9.4 rule 4
  gives a plan past the cap.
- **A stage never carries more than `MAX_STEPS_PER_STAGE` steps.** Through
  `stage_plan` the clamp is dormant while `Target` has only four reader
  values — the per-target rule bounds every stage first — but it guards the
  day `Target` grows past the cap.

Both drops are recorded, never silently re-routed.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from fittrack.graph.state import PlanStage, RouteStep, Target

MAX_STEPS_PER_STAGE = 4


@dataclass(frozen=True)
class StagedPlan:
    """What `stage_plan` produced: the stages plus every step the bounds dropped.

    The dropped steps come back as `errors` because the caller — the router
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
        stages.append(_capped(_one_per_target(writers, errors), errors))
    if readers:
        stages.append(_capped(_one_per_target(readers, errors), errors))

    return StagedPlan(stages=stages, errors=errors)


def _one_per_target(stage: list[RouteStep], errors: list[str]) -> PlanStage:
    """Keep the first step per target, recording each repeat in `errors`."""
    seen: set[Target] = set()
    kept: list[RouteStep] = []
    for step in stage:
        if step["target"] in seen:
            errors.append(
                f"router: a repeated step toward '{step['target']}' was dropped - "
                "single-writer key (spec 8.2)"
            )
            continue
        seen.add(step["target"])
        kept.append(step)
    return kept


def _capped(stage: PlanStage, errors: list[str]) -> PlanStage:
    """Truncate a stage to the cap, recording each dropped step in `errors`."""
    kept = stage[:MAX_STEPS_PER_STAGE]
    for step in stage[MAX_STEPS_PER_STAGE:]:
        errors.append(
            f"router: stage cap of {MAX_STEPS_PER_STAGE} steps dropped "
            f"the step toward '{step['target']}'"
        )
    return kept
