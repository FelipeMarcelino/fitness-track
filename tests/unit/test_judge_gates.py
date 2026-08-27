"""The blocking policy of spec section 21.2, proved without a live judge.

Every case here feeds pre-recorded verdicts into the gate function, so the CI
contract is testable with no provider credentials and no network.
"""

from __future__ import annotations

import pytest

from evals.judge.gates import evaluate_run
from evals.judge.models import CaseVerdict, RubricScore
from evals.judge.rubrics import Rubric, load_rubrics


@pytest.fixture(scope="module")
def rubrics() -> dict[str, Rubric]:
    return load_rubrics()


def verdict(case_id: str, **scores: int) -> CaseVerdict:
    return CaseVerdict(
        case_id=case_id,
        scores={
            name: RubricScore(rubric=name, score=score, justification="fixture")
            for name, score in scores.items()
        },
    )


PERFECT = {"safety": 5, "numeric_fidelity": 5, "profile_adherence": 5, "grounding": 5, "persona": 5}


def test_a_clean_round_passes(rubrics: dict[str, Rubric]) -> None:
    report = evaluate_run([verdict("b-1", **PERFECT)], rubrics=rubrics)
    assert report.blocking_failures == []
    assert report.exit_code == 0


@pytest.mark.parametrize("rubric", ["safety", "numeric_fidelity"])
@pytest.mark.parametrize("score", [1, 2, 3, 4])
def test_any_score_below_five_on_a_blocking_rubric_fails_the_round(
    rubrics: dict[str, Rubric], rubric: str, score: int
) -> None:
    report = evaluate_run([verdict("b-1", **{**PERFECT, rubric: score})], rubrics=rubrics)
    assert [f.rubric for f in report.blocking_failures] == [rubric]
    assert report.exit_code == 1


@pytest.mark.parametrize("rubric", ["profile_adherence", "grounding", "persona"])
def test_trend_rubrics_never_block(rubrics: dict[str, Rubric], rubric: str) -> None:
    report = evaluate_run([verdict("b-1", **{**PERFECT, rubric: 1})], rubrics=rubrics)
    assert report.blocking_failures == []
    assert report.exit_code == 0
    assert report.trends[rubric] == pytest.approx(1.0)


def test_channel_equivalence_does_not_block_before_phase_two(rubrics: dict[str, Rubric]) -> None:
    scored = verdict("b-1", **PERFECT, channel_equivalence=1)
    assert evaluate_run([scored], rubrics=rubrics, phase="1.0").exit_code == 0
    assert evaluate_run([scored], rubrics=rubrics, phase="2.0").exit_code == 1


def test_a_missing_blocking_rubric_is_itself_a_failure(rubrics: dict[str, Rubric]) -> None:
    """A judge that silently skips `safety` must not read as a pass."""
    incomplete = verdict("b-1", numeric_fidelity=5, profile_adherence=5, grounding=5, persona=5)
    report = evaluate_run([incomplete], rubrics=rubrics)
    assert [f.rubric for f in report.blocking_failures] == ["safety"]
    assert report.exit_code == 1


def test_trends_average_over_the_sample(rubrics: dict[str, Rubric]) -> None:
    report = evaluate_run(
        [verdict("b-1", **{**PERFECT, "persona": 3}), verdict("b-2", **{**PERFECT, "persona": 5})],
        rubrics=rubrics,
    )
    assert report.trends["persona"] == pytest.approx(4.0)
    assert report.scored_cases == 2
