"""The rubric set is a contract: spec section 21.2 fixes which rubrics block a merge."""

from __future__ import annotations

import pytest

from evals.judge.rubrics import Rubric, load_rubrics

BLOCKING = {"safety", "numeric_fidelity", "channel_equivalence"}
TREND = {"profile_adherence", "grounding", "persona"}


@pytest.fixture(scope="module")
def rubrics() -> dict[str, Rubric]:
    return load_rubrics()


def test_every_rubric_of_the_spec_exists(rubrics: dict[str, Rubric]) -> None:
    assert set(rubrics) == BLOCKING | TREND


def test_only_the_three_factual_rubrics_block(rubrics: dict[str, Rubric]) -> None:
    assert {name for name, r in rubrics.items() if r.blocking} == BLOCKING


def test_blocking_rubrics_demand_a_perfect_score(rubrics: dict[str, Rubric]) -> None:
    # Spec 21.2: "qualquer caso < 5" blocks. Anything short of 5 is a failure.
    for name in BLOCKING:
        assert rubrics[name].min_score == 5


def test_channel_equivalence_only_applies_from_phase_2(rubrics: dict[str, Rubric]) -> None:
    assert rubrics["channel_equivalence"].since_phase == "2.0"
    assert all(
        r.since_phase == "1.0" for name, r in rubrics.items() if name != "channel_equivalence"
    )


def test_each_rubric_carries_a_criterion_and_a_scale(rubrics: dict[str, Rubric]) -> None:
    for rubric in rubrics.values():
        assert rubric.criterion.strip()
        assert set(rubric.scale) == {1, 2, 3, 4, 5}


def test_active_rubrics_exclude_future_phases(rubrics: dict[str, Rubric]) -> None:
    from evals.judge.rubrics import active_rubrics

    assert set(active_rubrics(phase="1.0")) == (BLOCKING | TREND) - {"channel_equivalence"}
    assert set(active_rubrics(phase="2.0")) == BLOCKING | TREND


def test_only_the_answer_wide_rubrics_are_universal(rubrics: dict[str, Rubric]) -> None:
    """A universal rubric applies to every answer; no case may declare its way out.

    `channel_equivalence` blocks but is not universal: it needs two paired
    outputs, which a single-response case does not have.
    """
    assert {name for name, r in rubrics.items() if r.universal} == {"safety", "numeric_fidelity"}


def test_a_universal_rubric_is_always_blocking(rubrics: dict[str, Rubric]) -> None:
    assert all(r.blocking for r in rubrics.values() if r.universal)
