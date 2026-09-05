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


def test_baseline_examples_are_unambiguous_for_numeric_fidelity(
    baseline: list[JudgeCase],
) -> None:
    """Keep known borderline examples at a blocking-score-five standard."""
    responses = {case.id: case.response for case in baseline}

    assert "vai desacelerar" not in responses["base-004"]
    assert "uma ou duas sessões" not in responses["base-008"]
    assert "meta de 4 dias por semana" in responses["base-015"]
    assert "2.9 sessões por semana" in responses["base-016"]
    assert "RPE médio de 7.0 para 7.1" in responses["base-022"]
    assert "tendência registrada é de queda" in responses["base-003"]
    assert "84.0 kg de e1RM" in responses["base-006"]
    assert "faixa alta" not in responses["base-009"]
    assert "meta de 3 sessões por semana" in responses["base-016"]
    assert "densidade é alta" not in responses["base-017"]
    assert "85.0 kg × 6 repetições" in responses["base-025"]
    assert "3 dias por semana" in responses["base-033"]
    # Found by the first live CI round (PR #36): the judge blocked on
    # numeric_fidelity because these responses omitted a unit or pointed an
    # existing tool number at an origin the rubric does not allow.
    assert "52 séries de empurrar" in responses["base-011"]
    assert "meta de 4 dias por semana" in responses["base-014"]
    assert "6 séries de perna" in responses["base-028"]
    assert "meta de 5 sessões por semana" in responses["base-035"]
    # Second live round (PR #36): the judge's scale item 1 rejects a
    # prescription duration with no tool or user origin, and item 3 rejects a
    # causal tie the tool result does not demonstrate.
    assert "semana mais leve" not in responses["base-021"]
    assert "costuma pedir um deload" in responses["base-021"]
    assert "vem justamente" not in responses["base-028"]


def test_ids_are_unique(calibration: list[JudgeCase], baseline: list[JudgeCase]) -> None:
    ids = [case.id for case in calibration + baseline]
    assert len(ids) == len(set(ids))


@pytest.mark.parametrize("dataset", [CALIBRATION, BASELINE])
def test_every_case_grounds_its_numbers_in_tool_results(dataset: Path) -> None:
    """A case about numeric fidelity is meaningless without tool results to check against."""
    for case in load_cases(dataset):
        if "numeric_fidelity" in case.rubrics:
            assert case.tool_results, f"{case.id} is scored on numbers but supplies none"


def test_every_declared_rubric_exists(
    calibration: list[JudgeCase], baseline: list[JudgeCase]
) -> None:
    known = set(load_rubrics())
    for case in calibration + baseline:
        unknown = set(case.rubrics) - known
        assert not unknown, f"{case.id} declares unknown rubric(s): {sorted(unknown)}"


def test_a_misspelled_rubric_fails_to_load(tmp_path: Path) -> None:
    """Silently dropping it would remove a blocking rubric from the gate.

    At phase 2.0 a typo in `channel_equivalence` would take that rubric out of
    both scoring and missing-score enforcement, and a paired-channel case would
    pass without equivalence ever being checked.
    """
    import json

    path = tmp_path / "bad.jsonl"
    path.write_text(
        json.dumps(
            {
                "id": "x-1",
                "kind": "analysis",
                "user_message": "m",
                "response": "r",
                "rubrics": ["safety", "numeric_fidelty"],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="numeric_fidelty"):
        load_cases(path, known_rubrics=set(load_rubrics()))
