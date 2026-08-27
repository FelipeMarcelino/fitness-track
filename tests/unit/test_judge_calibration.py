"""Calibration policy of spec section 21.2.

Twenty cases with a known human grade run alongside the sample. More than two
judge errors discards the round and reports "judge nao calibrado" — it must not
fail the pull request, and it must not let a blocking failure through either.

Agreement is measured **per blocking rubric**, not per case. Collapsing the
human grades to one good/bad label would let a judge that always cries `safety`
agree with all twenty labels while being blind to numeric fidelity — and then be
trusted to wave numeric regressions through.
"""

from __future__ import annotations

import pytest

from evals.judge.calibration import calibrate
from evals.judge.gates import evaluate_run
from evals.judge.models import CaseVerdict, JudgeCase, RubricScore
from evals.judge.rubrics import Rubric, load_rubrics

RUBRICS = ["safety", "numeric_fidelity", "profile_adherence", "grounding", "persona"]
GOOD = {"safety": 5, "numeric_fidelity": 5, "profile_adherence": 5, "grounding": 5, "persona": 5}
BAD_SAFETY = {**GOOD, "safety": 1}
BAD_NUMERIC = {**GOOD, "numeric_fidelity": 1}


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


def case(case_id: str, human: dict[str, int]) -> JudgeCase:
    return JudgeCase(
        id=case_id,
        kind="analysis",
        user_message="m",
        response="r",
        rubrics=RUBRICS,
        label="bad" if any(human[n] < 5 for n in ("safety", "numeric_fidelity")) else "good",
        human_scores=human,
    )


def build() -> tuple[list[JudgeCase], dict[str, dict[str, int]]]:
    """Ten clean cases, five bad on safety, five bad on numeric fidelity."""
    human = {f"good-{i}": GOOD for i in range(10)}
    human |= {f"bad-safety-{i}": BAD_SAFETY for i in range(5)}
    human |= {f"bad-numeric-{i}": BAD_NUMERIC for i in range(5)}
    return [case(cid, scores) for cid, scores in human.items()], human


def agreeing() -> tuple[list[CaseVerdict], list[JudgeCase]]:
    cases, human = build()
    return [verdict(cid, scores) for cid, scores in human.items()], cases


def test_a_perfect_judge_is_calibrated(rubrics: dict[str, Rubric]) -> None:
    verdicts, cases = agreeing()
    result = calibrate(verdicts, cases=cases, rubrics=rubrics)
    assert result.mismatches == []
    assert result.calibrated


def test_two_errors_are_tolerated(rubrics: dict[str, Rubric]) -> None:
    verdicts, cases = agreeing()
    verdicts[0] = verdict("good-0", BAD_SAFETY)  # false positive
    verdicts[10] = verdict("bad-safety-0", GOOD)  # false negative
    result = calibrate(verdicts, cases=cases, rubrics=rubrics)
    assert sorted(result.mismatches) == ["bad-safety-0", "good-0"]
    assert result.calibrated


def test_three_errors_break_calibration(rubrics: dict[str, Rubric]) -> None:
    verdicts, cases = agreeing()
    for i in range(3):
        verdicts[i] = verdict(f"good-{i}", BAD_SAFETY)
    result = calibrate(verdicts, cases=cases, rubrics=rubrics)
    assert len(result.mismatches) == 3
    assert not result.calibrated


def test_a_judge_that_blames_the_wrong_rubric_is_not_calibrated(
    rubrics: dict[str, Rubric],
) -> None:
    """The failure a single good/bad label cannot see.

    This judge calls every bad case a safety violation. It agrees with all
    twenty labels and is blind to numeric fidelity — the exact blindness that
    would then let invented numbers through the sample.
    """
    verdicts, cases = agreeing()
    for i in range(5):
        verdicts[15 + i] = verdict(f"bad-numeric-{i}", BAD_SAFETY)
    result = calibrate(verdicts, cases=cases, rubrics=rubrics)
    assert len(result.mismatches) == 5
    assert not result.calibrated


def test_a_judge_that_fails_an_extra_blocking_rubric_is_not_calibrated(
    rubrics: dict[str, Rubric],
) -> None:
    """Right about safety, wrong about numbers, on a case that is only unsafe."""
    verdicts, cases = agreeing()
    verdicts[10] = verdict("bad-safety-0", {**BAD_SAFETY, "numeric_fidelity": 2})
    verdicts[11] = verdict("bad-safety-1", {**BAD_SAFETY, "numeric_fidelity": 2})
    verdicts[12] = verdict("bad-safety-2", {**BAD_SAFETY, "numeric_fidelity": 2})
    result = calibrate(verdicts, cases=cases, rubrics=rubrics)
    assert len(result.mismatches) == 3
    assert not result.calibrated


def test_a_trend_rubric_disagreement_does_not_count(rubrics: dict[str, Rubric]) -> None:
    """Only the factual rubrics calibrate; the gradual ones vary by design."""
    verdicts, cases = agreeing()
    verdicts[0] = verdict("good-0", {**GOOD, "persona": 2, "grounding": 1})
    result = calibrate(verdicts, cases=cases, rubrics=rubrics)
    assert result.mismatches == []


def test_a_missing_blocking_score_counts_as_a_failure(rubrics: dict[str, Rubric]) -> None:
    verdicts, cases = agreeing()
    verdicts[0] = verdict("good-0", {n: s for n, s in GOOD.items() if n != "safety"})
    result = calibrate(verdicts, cases=cases, rubrics=rubrics)
    assert result.mismatches == ["good-0"]


def test_an_uncalibrated_round_is_discarded_not_failed(rubrics: dict[str, Rubric]) -> None:
    verdicts, cases = agreeing()
    for i in range(3):
        verdicts[i] = verdict(f"good-{i}", BAD_SAFETY)
    result = calibrate(verdicts, cases=cases, rubrics=rubrics)

    report = evaluate_run([verdict("sample-1", BAD_SAFETY)], rubrics=rubrics, calibration=result)
    assert report.discarded
    assert report.exit_code == 0, "an uncalibrated judge must not fail the PR"
    assert report.blocking_failures, "the failures are still reported, just not enforced"


def test_a_calibrated_round_enforces_its_failures(rubrics: dict[str, Rubric]) -> None:
    verdicts, cases = agreeing()
    result = calibrate(verdicts, cases=cases, rubrics=rubrics)
    report = evaluate_run([verdict("sample-1", BAD_SAFETY)], rubrics=rubrics, calibration=result)
    assert not report.discarded
    assert report.exit_code == 1


def test_a_case_without_a_human_grade_is_refused(rubrics: dict[str, Rubric]) -> None:
    verdicts, cases = agreeing()
    ungraded = cases[0].model_copy(update={"human_scores": {}})
    with pytest.raises(ValueError, match="human"):
        calibrate(verdicts, cases=[ungraded, *cases[1:]], rubrics=rubrics)


def test_calibration_honours_the_rubrics_a_case_declares(rubrics: dict[str, Rubric]) -> None:
    """At phase 2.0 the channel rubric blocks, but only for cases that declare it.

    Without this, every single-response calibration case would be a mismatch for
    lacking a paired-channel score, and the judge would read as uncalibrated on
    the day phase 2.0 opens.
    """
    verdicts, cases = agreeing()
    result = calibrate(verdicts, cases=cases, rubrics=rubrics, phase="2.0")
    assert result.mismatches == []
    assert result.calibrated
