"""The deterministic golden set (§21.1).

Scored field by field against a floor per field, not as one overall average.
An average hides the failure this suite exists to catch: `exercise_slug`
collapsing from 0.92 to 0.60 while `is_workout_log` stays at 0.99 barely moves
the total, and the resolver is the thing that broke.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any, Final

from evals.dataset import Case, load_cases
from evals.report import SuiteResult

Predictor = Callable[[Case], dict[str, Any]]

# §21.1. Exact-match accuracy unless noted.
THRESHOLDS: Final[dict[str, float]] = {
    "is_workout_log": 0.98,
    "exercise_slug": 0.92,
    "load_kg": 0.97,
    "reps": 0.97,
    "set_count": 0.95,
    "route": 0.95,
    "guardrail_recall": 0.98,
}

# rpe is scored as mean absolute error, so it reads the other way round: lower
# is better, and the number is a ceiling rather than a floor.
RPE_MAX_ERROR: Final = 1.0

DEFAULT_THRESHOLD: Final = 0.95


def run_golden(path: Path, predict: Predictor) -> SuiteResult:
    """Scores a dataset and reports per-field accuracy.

    An empty dataset passes -- the harness ships with no LLM cases -- but says
    "no cases" rather than "all passed". Those two have to read differently, or
    a dataset that stopped being found looks exactly like success.
    """
    cases = load_cases(path)
    if not cases:
        return SuiteResult(name="golden", passed=True, total=0, summary=f"no cases in {path.name}")

    hits: dict[str, int] = {}
    seen: dict[str, int] = {}
    rpe_errors: list[float] = []
    failures: list[str] = []

    for case in cases:
        predicted = predict(case)
        for field_name, expected in case.expected.items():
            if field_name == "rpe":
                rpe_errors.append(_rpe_error(predicted.get("rpe"), expected))
                continue
            seen[field_name] = seen.get(field_name, 0) + 1
            # `predicted.get` rather than a membership check: a field the agent
            # stopped emitting has to count as wrong. Skipping it would score
            # the field perfectly over the cases it still answers.
            if predicted.get(field_name) == expected:
                hits[field_name] = hits.get(field_name, 0) + 1
            else:
                failures.append(
                    f"{case.id}: {field_name} expected {expected!r}, "
                    f"got {predicted.get(field_name)!r}"
                )

    below: list[str] = []
    for field_name, count in sorted(seen.items()):
        accuracy = hits.get(field_name, 0) / count
        floor = THRESHOLDS.get(field_name, DEFAULT_THRESHOLD)
        if accuracy < floor:
            below.append(f"{field_name} {accuracy:.3f} < {floor:.2f}")

    if rpe_errors:
        mean_error = sum(rpe_errors) / len(rpe_errors)
        if mean_error > RPE_MAX_ERROR:
            below.append(f"rpe mean absolute error {mean_error:.2f} > {RPE_MAX_ERROR:.1f}")

    summary = (
        f"{len(cases)} cases, all fields at or above their floor"
        if not below
        else f"{len(cases)} cases; below threshold: " + "; ".join(below)
    )
    return SuiteResult(
        name="golden",
        passed=not below,
        total=len(cases),
        summary=summary,
        failures=failures,
    )


def _rpe_error(predicted: Any, expected: Any) -> float:
    """A missing rpe is not a free pass.

    Treating it as absent would let an agent that dropped the field entirely
    post a mean error of zero over the cases it still answers.
    """
    try:
        return abs(float(predicted) - float(expected))
    except (TypeError, ValueError):
        # Anything unusable counts as a full miss on the 1-10 scale.
        return 10.0
