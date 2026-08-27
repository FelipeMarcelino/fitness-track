"""The CI blocking policy of spec section 21.2, as a pure function over verdicts.

Keeping this separate from the backend is what lets the policy be tested without
a live judge: the tests feed recorded verdicts in and assert the exit code.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from statistics import fmean

from evals.judge.models import CaseVerdict
from evals.judge.rubrics import Rubric, active_rubrics

DEFAULT_PHASE = "1.0"


@dataclass(frozen=True, slots=True)
class BlockingFailure:
    case_id: str
    rubric: str
    score: int | None
    justification: str

    def __str__(self) -> str:
        got = "not scored" if self.score is None else str(self.score)
        return f"{self.case_id} / {self.rubric}: {got} — {self.justification}"


@dataclass(frozen=True, slots=True)
class CalibrationResult:
    """Outcome of the twenty human-graded cases that run alongside the sample."""

    total: int
    mismatches: list[str]
    max_errors: int

    @property
    def calibrated(self) -> bool:
        return len(self.mismatches) <= self.max_errors


@dataclass(frozen=True, slots=True)
class RunReport:
    scored_cases: int
    phase: str
    blocking_failures: list[BlockingFailure] = field(default_factory=list)
    trends: dict[str, float] = field(default_factory=dict)
    calibration: CalibrationResult | None = None

    @property
    def discarded(self) -> bool:
        """An uncalibrated judge invalidates its own round (section 21.2)."""
        return self.calibration is not None and not self.calibration.calibrated

    @property
    def exit_code(self) -> int:
        if self.discarded:
            return 0
        return 1 if self.blocking_failures else 0


CaseRubrics = Mapping[str, list[str]]


def rubrics_for(
    case_id: str, rubrics: dict[str, Rubric], declared: CaseRubrics | None
) -> dict[str, Rubric]:
    """The rubrics one case is judged on.

    A case may narrow the set — an analysis answer has no retrieved material, so
    grading it on `grounding` produces a number that means nothing and still
    lands in the trend. What a case may **not** do is narrow away a *universal*
    rubric: safety and numeric fidelity apply to every answer, and letting a
    fixture opt out of them would turn the declaration into a way to skip the
    gate.

    `channel_equivalence` blocks but is not universal: it compares two paired
    outputs, and a single-response case has nothing to compare. Forcing it on
    every case would fail the whole suite the day phase 2.0 opens.
    """
    if declared is None or case_id not in declared:
        return rubrics
    chosen = set(declared[case_id])
    return {name: rubric for name, rubric in rubrics.items() if name in chosen or rubric.universal}


def blocking_failures(
    verdicts: list[CaseVerdict],
    rubrics: dict[str, Rubric],
    case_rubrics: CaseRubrics | None = None,
) -> list[BlockingFailure]:
    failures: list[BlockingFailure] = []
    for verdict in verdicts:
        for name, rubric in rubrics_for(verdict.case_id, rubrics, case_rubrics).items():
            if not rubric.blocking:
                continue
            scored = verdict.scores.get(name)
            if scored is None:
                # A judge that skipped a blocking rubric has not cleared it.
                failures.append(
                    BlockingFailure(verdict.case_id, name, None, "rubric was not scored")
                )
            elif rubric.fails(scored.score):
                failures.append(
                    BlockingFailure(verdict.case_id, name, scored.score, scored.justification)
                )
    return failures


def trend_averages(
    verdicts: list[CaseVerdict],
    rubrics: dict[str, Rubric],
    case_rubrics: CaseRubrics | None = None,
) -> dict[str, float]:
    """Mean score per non-blocking rubric; the series plotted per prompt version.

    Only cases that declare a rubric contribute to its average, so a case scored
    on something it never had material for cannot drag the series.
    """
    trends: dict[str, float] = {}
    for name, rubric in rubrics.items():
        if rubric.blocking:
            continue
        scores = [
            s.score
            for v in verdicts
            if name in rubrics_for(v.case_id, rubrics, case_rubrics)
            and (s := v.scores.get(name)) is not None
        ]
        if scores:
            trends[name] = fmean(scores)
    return trends


def evaluate_run(
    verdicts: list[CaseVerdict],
    *,
    rubrics: dict[str, Rubric],
    phase: str = DEFAULT_PHASE,
    calibration: CalibrationResult | None = None,
    case_rubrics: CaseRubrics | None = None,
) -> RunReport:
    """Turn a round of verdicts into a CI decision."""
    # The phase filter comes first and a case declaration cannot widen it:
    # channel equivalence has nothing to compare against before phase 2.0.
    applicable = active_rubrics(phase, rubrics)
    return RunReport(
        scored_cases=len(verdicts),
        phase=phase,
        blocking_failures=blocking_failures(verdicts, applicable, case_rubrics),
        trends=trend_averages(verdicts, applicable, case_rubrics),
        calibration=calibration,
    )
