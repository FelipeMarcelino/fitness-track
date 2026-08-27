"""Calibration of the judge itself (spec section 21.2).

Twenty cases with a known human grade — half clearly good, half clearly bad —
run alongside every sample. The derived label of a case is `bad` when any active
blocking rubric scored below its minimum, and `good` otherwise; disagreement
with the human label is an error. More than two errors discard the round.
"""

from __future__ import annotations

from collections.abc import Mapping

from evals.judge.gates import DEFAULT_PHASE, CalibrationResult
from evals.judge.models import CaseVerdict, JudgeCase, Label
from evals.judge.rubrics import Rubric, active_rubrics

MAX_CALIBRATION_ERRORS = 2
CALIBRATION_SIZE = 20


def derived_label(verdict: CaseVerdict, rubrics: Mapping[str, Rubric]) -> Label:
    for name, rubric in rubrics.items():
        if not rubric.blocking:
            continue
        score = verdict.score_of(name)
        if score is None or rubric.fails(score):
            return "bad"
    return "good"


def human_labels(cases: list[JudgeCase]) -> dict[str, Label]:
    labels: dict[str, Label] = {}
    for case in cases:
        if case.label is None:
            raise ValueError(f"calibration case {case.id} has no human label")
        labels[case.id] = case.label
    return labels


def calibrate(
    verdicts: list[CaseVerdict],
    *,
    human_labels: Mapping[str, Label],
    rubrics: dict[str, Rubric],
    phase: str = DEFAULT_PHASE,
    max_errors: int = MAX_CALIBRATION_ERRORS,
) -> CalibrationResult:
    applicable = active_rubrics(phase, rubrics)
    mismatches = [
        verdict.case_id
        for verdict in verdicts
        if verdict.case_id in human_labels
        and derived_label(verdict, applicable) != human_labels[verdict.case_id]
    ]
    return CalibrationResult(total=len(verdicts), mismatches=mismatches, max_errors=max_errors)
