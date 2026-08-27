"""Calibration policy of spec section 21.2.

Twenty cases with a known human grade run alongside the sample. More than two
judge errors discards the round and reports "judge nao calibrado" — it must not
fail the pull request, and it must not let a blocking failure through either.
"""

from __future__ import annotations

import pytest

from evals.judge.calibration import calibrate
from evals.judge.gates import evaluate_run
from evals.judge.models import CaseVerdict, Label, RubricScore
from evals.judge.rubrics import Rubric, load_rubrics

GOOD = {"safety": 5, "numeric_fidelity": 5, "profile_adherence": 5, "grounding": 5, "persona": 5}
BAD_SAFETY = {**GOOD, "safety": 1}


@pytest.fixture(scope="module")
def rubrics() -> dict[str, Rubric]:
    return load_rubrics()


def verdict(case_id: str, scores: dict[str, int]) -> CaseVerdict:
    return CaseVerdict(
        case_id=case_id,
        scores={
            name: RubricScore(rubric=name, score=score, justification="fixture")
            for name, score in scores.items()
        },
    )


def build(n_good: int, n_bad: int) -> tuple[list[CaseVerdict], dict[str, Label]]:
    verdicts: list[CaseVerdict] = []
    labels: dict[str, Label] = {}
    for i in range(n_good):
        verdicts.append(verdict(f"good-{i}", GOOD))
        labels[f"good-{i}"] = "good"
    for i in range(n_bad):
        verdicts.append(verdict(f"bad-{i}", BAD_SAFETY))
        labels[f"bad-{i}"] = "bad"
    return verdicts, labels


def test_a_perfect_judge_is_calibrated(rubrics: dict[str, Rubric]) -> None:
    verdicts, labels = build(10, 10)
    result = calibrate(verdicts, human_labels=labels, rubrics=rubrics)
    assert result.mismatches == []
    assert result.calibrated


def test_two_errors_are_tolerated(rubrics: dict[str, Rubric]) -> None:
    verdicts, labels = build(10, 10)
    verdicts[0] = verdict("good-0", BAD_SAFETY)  # false positive
    verdicts[10] = verdict("bad-0", GOOD)  # false negative
    result = calibrate(verdicts, human_labels=labels, rubrics=rubrics)
    assert sorted(result.mismatches) == ["bad-0", "good-0"]
    assert result.calibrated


def test_three_errors_break_calibration(rubrics: dict[str, Rubric]) -> None:
    verdicts, labels = build(10, 10)
    for i in range(3):
        verdicts[i] = verdict(f"good-{i}", BAD_SAFETY)
    result = calibrate(verdicts, human_labels=labels, rubrics=rubrics)
    assert len(result.mismatches) == 3
    assert not result.calibrated


def test_an_uncalibrated_round_is_discarded_not_failed(rubrics: dict[str, Rubric]) -> None:
    verdicts, labels = build(10, 10)
    for i in range(3):
        verdicts[i] = verdict(f"good-{i}", BAD_SAFETY)
    result = calibrate(verdicts, human_labels=labels, rubrics=rubrics)

    report = evaluate_run([verdict("sample-1", BAD_SAFETY)], rubrics=rubrics, calibration=result)
    assert report.discarded
    assert report.exit_code == 0, "an uncalibrated judge must not fail the PR"
    assert report.blocking_failures, "the failures are still reported, just not enforced"


def test_a_calibrated_round_enforces_its_failures(rubrics: dict[str, Rubric]) -> None:
    verdicts, labels = build(10, 10)
    result = calibrate(verdicts, human_labels=labels, rubrics=rubrics)
    report = evaluate_run([verdict("sample-1", BAD_SAFETY)], rubrics=rubrics, calibration=result)
    assert not report.discarded
    assert report.exit_code == 1
