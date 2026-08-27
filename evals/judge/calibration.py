"""Calibration of the judge itself (spec section 21.2).

Twenty cases with a known human grade — half clearly good, half clearly bad —
run alongside every sample. More than two errors discard the round: the CI
reports "judge nao calibrado" and does not reprove the PR. Without this, a
change of judge model would pass for a regression of the product.

**Agreement is measured per blocking rubric, not per case.** Collapsing the
human grades into one good/bad label hides the failure that matters most: a
judge that answers `safety=1` to every bad case agrees with all twenty labels
while being completely blind to numeric fidelity — and is then trusted to let
the sample's numeric answers through.
"""

from __future__ import annotations

from collections.abc import Mapping

from evals.judge.gates import DEFAULT_PHASE, CalibrationResult, rubrics_for
from evals.judge.models import CaseVerdict, JudgeCase, Label
from evals.judge.rubrics import Rubric, active_rubrics

MAX_CALIBRATION_ERRORS = 2
CALIBRATION_SIZE = 20


def derived_label(verdict: CaseVerdict, rubrics: Mapping[str, Rubric]) -> Label:
    """How the judge classifies a case: `bad` if any blocking rubric failed."""
    for name, rubric in rubrics.items():
        if not rubric.blocking:
            continue
        score = verdict.score_of(name)
        if score is None or rubric.fails(score):
            return "bad"
    return "good"


def _failed(scores: Mapping[str, int], name: str, rubric: Rubric) -> bool:
    score = scores.get(name)
    # A rubric with no score has not been cleared, by either judge or human.
    return score is None or rubric.fails(score)


def disagreements(
    verdict: CaseVerdict, case: JudgeCase, rubrics: Mapping[str, Rubric]
) -> list[str]:
    """Blocking rubrics where the judge and the human disagree on pass/fail."""
    judge_scores = {name: scored.score for name, scored in verdict.scores.items()}
    return [
        name
        for name, rubric in rubrics.items()
        if rubric.blocking
        and _failed(judge_scores, name, rubric) != _failed(case.human_scores, name, rubric)
    ]


def calibrate(
    verdicts: list[CaseVerdict],
    *,
    cases: list[JudgeCase],
    rubrics: dict[str, Rubric],
    phase: str = DEFAULT_PHASE,
    max_errors: int = MAX_CALIBRATION_ERRORS,
) -> CalibrationResult:
    applicable = active_rubrics(phase, rubrics)
    by_id = {case.id: case for case in cases}

    missing = [case.id for case in cases if not case.human_scores]
    if missing:
        raise ValueError(f"calibration cases without a human grade: {sorted(missing)}")

    declared = {case.id: case.rubrics for case in cases if case.rubrics}
    mismatches = [
        verdict.case_id
        for verdict in verdicts
        if verdict.case_id in by_id
        and disagreements(
            verdict,
            by_id[verdict.case_id],
            rubrics_for(verdict.case_id, applicable, declared),
        )
    ]
    return CalibrationResult(total=len(verdicts), mismatches=mismatches, max_errors=max_errors)
