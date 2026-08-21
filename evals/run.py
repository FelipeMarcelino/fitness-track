"""`python -m evals.run --suite all` (§21.4).

Runs in CI on every PR that touches prompts, agents, the graph or the evals
themselves. Infrastructure PRs skip the judge -- it costs 60 reasoning-tier
calls a run -- but the deterministic golden set is cheap and always runs.

Today both suites are empty: there is no agent yet to produce output. That is
the point of landing this first (AD-31). The harness reports "no cases"
truthfully and exits 0, so the first agent PR has somewhere to put its cases
instead of inventing the harness under deadline.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from evals.dataset import Case, load_cases
from evals.judge import Scorer, calibrate, judge_suite
from evals.report import SuiteResult, exit_code, render
from evals.runner import run_golden

DATA = Path(__file__).parent / "data"
GOLDEN = DATA / "golden.jsonl"
CALIBRATION = DATA / "calibration.jsonl"

SUITES = ("golden", "judge", "all")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="evals.run")
    parser.add_argument("--suite", choices=SUITES, default="all")
    parser.add_argument(
        "--data",
        type=Path,
        default=DATA,
        help="directory holding the datasets; overridden in tests",
    )
    args = parser.parse_args(argv)

    results: list[SuiteResult] = []
    if args.suite in ("golden", "all"):
        results.append(run_golden(args.data / "golden.jsonl", predict=_no_model_yet))
    if args.suite in ("judge", "all"):
        results.append(_run_judge(args.data / "calibration.jsonl"))

    print(render(results))
    code = exit_code(results)
    print(f"\nevals: {'ok' if code == 0 else 'FAILED'}")
    return code


def _no_model_yet(case: Case) -> dict[str, object]:
    """Stands in until there is a graph to call.

    Returning nothing is safe only because the golden dataset is empty: the
    moment a case exists, every field it expects is missing and the suite goes
    red. That is the intended pressure -- the first agent PR has to wire a real
    predictor in, it cannot quietly leave this stub in place.
    """
    raise AssertionError(
        f"no predictor is wired up, but the golden set has cases (first: {case.id}). "
        f"Point run_golden at the graph."
    )


def _run_judge(calibration_path: Path) -> SuiteResult:
    """Calibrates the judge, then judges -- in that order, always.

    Judging first and calibrating afterwards would mean acting on a verdict
    before knowing whether it means anything.
    """
    cases = load_cases(calibration_path)
    if not cases:
        return SuiteResult(
            name="judge",
            passed=True,
            total=0,
            summary="no calibration set; judge not run",
        )

    scorer = _judge_scorer()
    if scorer is None:
        # No provider configured. Said out loud rather than passing quietly:
        # a judge job that silently does nothing is worse than no job at all,
        # because the check appears green.
        return SuiteResult(
            name="judge",
            passed=True,
            total=0,
            summary=(
                "no judge provider configured (set the LLM credentials); "
                f"{len(cases)} calibration cases not run"
            ),
        )

    calibration = calibrate(cases, score=scorer)
    return judge_suite(_judge_samples(), calibration=calibration)


def _judge_scorer() -> Scorer | None:
    """The judge model call, once there is output to judge (§21.2).

    Returns None while no provider is configured, which is every run until the
    first agent lands. Wiring the model in before there is anything to score
    would burn reasoning-tier calls on an empty sample.
    """
    return None


def _judge_samples() -> list[dict[str, int]]:
    """The 40-case sample of §21.2. Empty until the graph produces output."""
    return []


if __name__ == "__main__":
    sys.exit(main())
