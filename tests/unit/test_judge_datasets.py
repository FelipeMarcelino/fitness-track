"""The versioned judge datasets are a contract too.

Spec 21.2 fixes the shape of the calibration set: twenty cases, half clearly
good and half clearly bad. The baseline is the forty-case sample of a round.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from evals.judge.calibration import CALIBRATION_SIZE
from evals.judge.datasets import BASELINE, CALIBRATION, load_cases
from evals.judge.models import JudgeCase
from evals.judge.rubrics import load_rubrics

BASELINE_SIZE = 40


@pytest.fixture(scope="module")
def calibration() -> list[JudgeCase]:
    return load_cases(CALIBRATION)


@pytest.fixture(scope="module")
def baseline() -> list[JudgeCase]:
    return load_cases(BASELINE)


def test_calibration_has_twenty_cases(calibration: list[JudgeCase]) -> None:
    assert len(calibration) == CALIBRATION_SIZE


def test_calibration_is_balanced(calibration: list[JudgeCase]) -> None:
    labels = [case.label for case in calibration]
    assert labels.count("good") == labels.count("bad") == CALIBRATION_SIZE // 2


def test_every_calibration_case_carries_human_scores(calibration: list[JudgeCase]) -> None:
    rubrics = load_rubrics()
    for case in calibration:
        assert case.human_scores, f"{case.id} has no human grade"
        assert set(case.human_scores) <= set(rubrics), f"{case.id} grades an unknown rubric"
        assert all(1 <= v <= 5 for v in case.human_scores.values())


def test_a_bad_case_fails_a_blocking_rubric_and_a_good_case_does_not(
    calibration: list[JudgeCase],
) -> None:
    rubrics = load_rubrics()
    blocking = {name for name, r in rubrics.items() if r.blocking}
    for case in calibration:
        failed = {n for n, s in case.human_scores.items() if n in blocking and s < 5}
        if case.label == "bad":
            assert failed, f"{case.id} is labelled bad but clears every blocking rubric"
        else:
            assert not failed, f"{case.id} is labelled good but fails {sorted(failed)}"


def test_calibration_covers_both_blocking_rubrics(calibration: list[JudgeCase]) -> None:
    violated = {
        name
        for case in calibration
        if case.label == "bad"
        for name, score in case.human_scores.items()
        if score < 5
    }
    assert {"safety", "numeric_fidelity"} <= violated


def test_baseline_is_a_sample_of_forty(baseline: list[JudgeCase]) -> None:
    assert len(baseline) == BASELINE_SIZE


def test_baseline_carries_no_human_grade(baseline: list[JudgeCase]) -> None:
    # The baseline is what the judge scores; grading it here would be circular.
    for case in baseline:
        assert not case.human_scores
        assert case.label is None


def test_ids_are_unique(calibration: list[JudgeCase], baseline: list[JudgeCase]) -> None:
    ids = [case.id for case in calibration + baseline]
    assert len(ids) == len(set(ids))


@pytest.mark.parametrize("dataset", [CALIBRATION, BASELINE])
def test_every_case_grounds_its_numbers_in_tool_results(dataset: Path) -> None:
    """A case about numeric fidelity is meaningless without tool results to check against."""
    for case in load_cases(dataset):
        if "numeric_fidelity" in case.rubrics:
            assert case.tool_results, f"{case.id} is scored on numbers but supplies none"
