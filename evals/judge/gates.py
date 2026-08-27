"""The CI blocking policy of spec section 21.2, as a pure function over verdicts.

Keeping this separate from the backend is what lets the policy be tested without
a live judge: the tests feed recorded verdicts in and assert the exit code.
"""

from __future__ import annotations

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


def _applicable(verdict: CaseVerdict, rubrics: dict[str, Rubric]) -> dict[str, Rubric]:
    """Rubrics a case must be scored on: every active one it was or should be scored on."""
    return rubrics


def blocking_failures(
    verdicts: list[CaseVerdict], rubrics: dict[str, Rubric]
) -> list[BlockingFailure]:
    failures: list[BlockingFailure] = []
    for verdict in verdicts:
        for name, rubric in _applicable(verdict, rubrics).items():
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


def trend_averages(verdicts: list[CaseVerdict], rubrics: dict[str, Rubric]) -> dict[str, float]:
    """Mean score per non-blocking rubric; the series plotted per prompt version."""
    trends: dict[str, float] = {}
    for name, rubric in rubrics.items():
        if rubric.blocking:
            continue
        scores = [s.score for v in verdicts if (s := v.scores.get(name)) is not None]
        if scores:
            trends[name] = fmean(scores)
    return trends


def evaluate_run(
    verdicts: list[CaseVerdict],
    *,
    rubrics: dict[str, Rubric],
    phase: str = DEFAULT_PHASE,
    calibration: CalibrationResult | None = None,
) -> RunReport:
    """Turn a round of verdicts into a CI decision."""
    applicable = active_rubrics(phase, rubrics)
    return RunReport(
        scored_cases=len(verdicts),
        phase=phase,
        blocking_failures=blocking_failures(verdicts, applicable),
        trends=trend_averages(verdicts, applicable),
        calibration=calibration,
    )
