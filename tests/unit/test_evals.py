"""The evaluation harness (§21).

It exists before the first agent for the reason §21.2 gives: waiting for
"enough code" to evaluate is writing tests last, and by the time it arrives
the regression is already there and nobody knows which change caused it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from evals.dataset import Case, load_cases
from evals.judge import BLOCKING_RUBRICS, CalibrationOutcome, calibrate, judge_suite
from evals.report import SuiteResult, exit_code
from evals.runner import run_golden


def _write(tmp_path: Path, name: str, rows: list[dict[str, Any]]) -> Path:
    path = tmp_path / name
    path.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n")
    return path


# --- dataset ---------------------------------------------------------------


def test_an_empty_dataset_is_empty_not_an_error(tmp_path: Path) -> None:
    """Distinct from a broken one. The harness ships with no LLM cases, and
    that has to be a legible state rather than a crash."""
    path = tmp_path / "empty.jsonl"
    path.write_text("")
    assert load_cases(path) == []


def test_a_missing_dataset_is_an_error(tmp_path: Path) -> None:
    """The silent-zero failure mode: a renamed file would otherwise report a
    perfect score over nothing."""
    with pytest.raises(FileNotFoundError):
        load_cases(tmp_path / "nope.jsonl")


def test_blank_lines_are_skipped_but_bad_json_is_not(tmp_path: Path) -> None:
    path = tmp_path / "mixed.jsonl"
    path.write_text('{"id":"a","input":"x","expected":{}}\n\n{not json}\n')
    with pytest.raises(ValueError, match="line 3"):
        load_cases(path)


# --- golden set ------------------------------------------------------------


def test_a_suite_with_no_cases_passes_and_says_so(tmp_path: Path) -> None:
    """Exit 0, but never silently: "no cases" and "everything passed" have to
    read differently or a lost dataset looks like success."""
    path = _write(tmp_path, "golden.jsonl", [])
    result = run_golden(path, predict=lambda _case: {})

    assert result.passed
    assert result.total == 0
    assert "no cases" in result.summary.lower()
    assert exit_code([result]) == 0


def test_a_failing_case_fails_the_suite(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "golden.jsonl",
        [
            {"id": "gs-1", "input": "supino 80x8", "expected": {"is_workout_log": True}},
            {"id": "gs-2", "input": "oi", "expected": {"is_workout_log": False}},
        ],
    )

    def predict(case: Case) -> dict[str, Any]:
        # Wrong on gs-2: everything looks like a workout log.
        return {"is_workout_log": True}

    result = run_golden(path, predict=predict)

    assert not result.passed
    # The failing case is named, so the summary stays readable and the detail
    # is still there for whoever has to fix it.
    assert any("gs-2" in failure for failure in result.failures)
    assert "gs-1" not in " ".join(result.failures)
    assert "is_workout_log" in result.summary
    assert exit_code([result]) != 0


def test_a_field_below_its_threshold_fails_even_with_most_cases_right(
    tmp_path: Path,
) -> None:
    """§21.1 sets a floor per field, not an overall average. Averaging lets a
    field collapse while the total still looks healthy."""
    rows = [
        {"id": f"gs-{i}", "input": "x", "expected": {"exercise_slug": "supino_reto_barra"}}
        for i in range(100)
    ]
    path = _write(tmp_path, "golden.jsonl", rows)

    calls = {"n": 0}

    def predict(case: Case) -> dict[str, Any]:
        calls["n"] += 1
        # 90% right: above nothing in particular, below the 0.92 floor.
        if calls["n"] <= 90:
            return {"exercise_slug": "supino_reto_barra"}
        return {"exercise_slug": "agachamento_livre"}

    result = run_golden(path, predict=predict)

    assert not result.passed
    assert "exercise_slug" in result.summary


def test_a_field_nobody_predicted_is_a_failure_not_a_skip(tmp_path: Path) -> None:
    """An agent that stops emitting a field would otherwise score perfectly on
    it, because there is nothing to compare and nothing to notice."""
    path = _write(
        tmp_path,
        "golden.jsonl",
        [{"id": "gs-1", "input": "x", "expected": {"load_kg": 80}}],
    )
    result = run_golden(path, predict=lambda _case: {})

    assert not result.passed


# --- judge calibration -----------------------------------------------------


def _calibration_cases() -> list[Case]:
    return [
        Case(id=f"cal-{i}", input="...", expected={"human_score": 5 if i < 10 else 1})
        for i in range(20)
    ]


def test_a_calibrated_judge_lets_the_round_stand() -> None:
    def perfect(case: Case) -> int:
        return int(case.expected["human_score"])

    outcome = calibrate(_calibration_cases(), score=perfect)
    assert outcome.calibrated
    assert outcome.errors == 0


def test_two_mistakes_are_tolerated() -> None:
    """§21.2 allows up to 2. The judge has variance and a hair trigger here
    would discard good rounds."""
    wrong = {"cal-0", "cal-1"}

    def slightly_off(case: Case) -> int:
        return 1 if case.id in wrong else int(case.expected["human_score"])

    outcome = calibrate(_calibration_cases(), score=slightly_off)
    assert outcome.calibrated
    assert outcome.errors == 2


def test_an_uncalibrated_judge_discards_the_round_without_failing_the_pr() -> None:
    """The subtlety of §21.2. A judge that changed underneath us says nothing
    about this PR, so blocking on it would punish the wrong change -- and the
    natural reaction, re-running until green, destroys the signal entirely.
    """
    wrong = {f"cal-{i}" for i in range(5)}

    def broken(case: Case) -> int:
        return 1 if case.id in wrong else int(case.expected["human_score"])

    outcome = calibrate(_calibration_cases(), score=broken)

    assert not outcome.calibrated
    assert outcome.errors == 5
    assert outcome.outcome is CalibrationOutcome.DISCARDED

    result = judge_suite([], calibration=outcome)
    assert result.passed, "an uncalibrated judge must not fail the PR"
    assert "not calibrated" in result.summary.lower()
    assert exit_code([result]) == 0


# --- judge rubrics ---------------------------------------------------------


def test_safety_and_numeric_fidelity_block_the_merge() -> None:
    """The two rubrics where §21.2 says the judge agrees with a human: the
    question is factual, not aesthetic."""
    assert frozenset({"safety", "numeric_fidelity"}) == BLOCKING_RUBRICS


def test_a_single_unsafe_case_fails_the_suite() -> None:
    scores = [
        {"safety": 5, "numeric_fidelity": 5, "persona": 3},
        {"safety": 4, "numeric_fidelity": 5, "persona": 5},
    ]
    calibrated = calibrate(_calibration_cases(), score=lambda c: int(c.expected["human_score"]))
    result = judge_suite(scores, calibration=calibrated)

    assert not result.passed
    assert "safety" in result.summary


def test_a_low_persona_score_is_a_trend_not_a_block() -> None:
    """Blocking on a graded aesthetic judgement produces red CI from noise, and
    the reaction to noisy CI is to stop reading it."""
    scores = [{"safety": 5, "numeric_fidelity": 5, "persona": 1, "grounding": 2}]
    calibrated = calibrate(_calibration_cases(), score=lambda c: int(c.expected["human_score"]))
    result = judge_suite(scores, calibration=calibrated)

    assert result.passed


def test_an_invented_number_fails_regardless_of_the_rest() -> None:
    """§1.1's central invariant: the bot never states a number it did not read
    from a tool result."""
    scores = [{"safety": 5, "numeric_fidelity": 1, "persona": 5}]
    calibrated = calibrate(_calibration_cases(), score=lambda c: int(c.expected["human_score"]))
    result = judge_suite(scores, calibration=calibrated)

    assert not result.passed
    assert "numeric_fidelity" in result.summary


def test_a_missing_blocking_rubric_is_not_a_pass() -> None:
    """A judge that stopped returning the safety score would otherwise sail
    through -- the most dangerous way for this suite to break."""
    scores = [{"numeric_fidelity": 5, "persona": 5}]
    calibrated = calibrate(_calibration_cases(), score=lambda c: int(c.expected["human_score"]))
    result = judge_suite(scores, calibration=calibrated)

    assert not result.passed
    assert "safety" in result.summary


# --- exit codes ------------------------------------------------------------


def test_one_failing_suite_fails_the_run() -> None:
    passing = SuiteResult(name="golden", passed=True, total=10, summary="ok")
    failing = SuiteResult(name="judge", passed=False, total=1, summary="bad")
    assert exit_code([passing, failing]) != 0
    assert exit_code([passing]) == 0


# --- the CLI ---------------------------------------------------------------


def test_the_cli_runs_green_over_empty_suites(capsys: pytest.CaptureFixture[str]) -> None:
    """`python -m evals.run --suite all` has to pass in CI today (§21.4).

    There is no agent yet, so both suites are empty. Exit 0 -- but the output
    has to say "no cases", because a check that is green over nothing is how a
    lost dataset goes unnoticed for a month.
    """
    from evals.run import main

    assert main(["--suite", "all"]) == 0
    output = capsys.readouterr().out
    assert "no cases" in output
    assert "not run" in output


def test_the_stub_predictor_refuses_to_score_real_cases(tmp_path: Path) -> None:
    """The pressure that keeps the stub from surviving into production.

    A stub that returned {} would score every case wrong -- red CI, fixable by
    deleting cases. One that returned the expected answer would be worse. It
    raises instead, so the first agent PR has to wire a real predictor.
    """
    from evals.run import _no_model_yet

    with pytest.raises(AssertionError, match="no predictor"):
        _no_model_yet(Case(id="gs-1", input="x", expected={"reps": 8}))


def test_the_shipped_calibration_set_is_the_one_the_spec_describes() -> None:
    """§21.2: 20 cases, half clearly good and half clearly bad. Unbalanced, a
    judge that answered "bad" to everything would calibrate perfectly."""
    from evals.run import CALIBRATION

    cases = load_cases(CALIBRATION)
    assert len(cases) == 20
    good = [case for case in cases if int(case.expected["human_score"]) >= 4]
    bad = [case for case in cases if int(case.expected["human_score"]) <= 2]
    assert len(good) == 10
    assert len(bad) == 10
    assert len({case.id for case in cases}) == 20


def test_a_judge_that_answers_the_same_thing_every_time_fails_calibration() -> None:
    """The reason the set is balanced. A constant answer scores 10/20, which is
    well past the limit, so the round is discarded rather than trusted."""
    from evals.run import CALIBRATION

    cases = load_cases(CALIBRATION)
    outcome = calibrate(cases, score=lambda _case: 1)

    assert not outcome.calibrated
    assert outcome.errors == 10
