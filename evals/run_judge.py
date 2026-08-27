#!/usr/bin/env python3
"""Run the LLM-as-judge round of spec section 21.2.

    python -m evals.run_judge                       # live judge, needs credentials
    python -m evals.run_judge --backend replay \
        --verdicts recorded.jsonl                   # offline, for tests and reruns

Run it as a module, not as a path: `evals` is a package and the repo root has to
be on `sys.path` for the imports below to resolve.

Exit code 0 means the round did not block the merge; 1 means a blocking rubric
failed on a calibrated judge. An uncalibrated judge discards its own round and
still exits 0 — it reports, it does not reprove (section 21.2).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from evals.judge.backends import (
    CREDENTIAL_ENV,
    DEFAULT_JUDGE_MODEL,
    AnthropicBackend,
    JudgeBackend,
    MissingCredentialsError,
    ReplayBackend,
)
from evals.judge.calibration import calibrate, human_labels
from evals.judge.datasets import BASELINE, CALIBRATION, load_cases
from evals.judge.gates import DEFAULT_PHASE, CaseRubrics, evaluate_run, rubrics_for
from evals.judge.models import CaseVerdict, JudgeCase
from evals.judge.report import render
from evals.judge.rubrics import Rubric, active_rubrics, load_rubrics


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend", choices=["auto", "anthropic", "replay"], default="auto")
    parser.add_argument("--verdicts", type=Path, help="recorded verdicts, for --backend replay")
    parser.add_argument("--out", type=Path, help="write this round's verdicts as JSONL")
    parser.add_argument("--phase", default=DEFAULT_PHASE, help="roadmap phase (section 24)")
    parser.add_argument("--model", default=DEFAULT_JUDGE_MODEL)
    parser.add_argument("--baseline", type=Path, default=BASELINE)
    parser.add_argument("--calibration", type=Path, default=CALIBRATION)
    return parser.parse_args(argv)


def _build_backend(args: argparse.Namespace) -> JudgeBackend | None:
    if args.backend == "replay":
        if args.verdicts is None:
            raise SystemExit("--backend replay requires --verdicts")
        return ReplayBackend.from_file(args.verdicts)
    try:
        return AnthropicBackend(model=args.model)
    except MissingCredentialsError:
        if args.backend == "anthropic":
            # Strict mode is what CI asks for. Reporting and exiting 0 here would
            # turn a required check green over a diff nothing scored.
            return None
        return None


def _score_all(
    backend: JudgeBackend,
    cases: list[JudgeCase],
    rubrics: dict[str, Rubric],
    case_rubrics: CaseRubrics,
) -> list[CaseVerdict]:
    """Ask the backend only for the rubrics each case is actually gated on."""
    return [backend.score(case, rubrics_for(case.id, rubrics, case_rubrics)) for case in cases]


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    backend = _build_backend(args)
    if backend is None:
        message = (
            f"LLM-as-judge nao executado: {CREDENTIAL_ENV} ausente.\n"
            f"Defina {CREDENTIAL_ENV} para rodar o judge ao vivo, ou use\n"
            f"  python -m evals.run_judge --backend replay --verdicts <arquivo>\n"
            f"para reavaliar uma rodada gravada."
        )
        if args.backend == "anthropic":
            # Strict mode: CI asked for a live judge and did not get one. The
            # only honest outcome is a red check — a green one would say the
            # diff was scored for safety and numeric fidelity when it was not.
            print(message, file=sys.stderr)
            return 1
        # Tolerant mode, for a local run: reported, never a silent pass.
        print(message)
        return 0

    rubrics = active_rubrics(args.phase, load_rubrics())
    calibration_cases = load_cases(args.calibration)
    baseline_cases = load_cases(args.baseline)

    # A case may narrow the rubrics it is scored on: grading an analysis answer
    # on `grounding` when it retrieved nothing produces a number that means
    # nothing and still lands in the trend.
    case_rubrics = {
        case.id: case.rubrics for case in calibration_cases + baseline_cases if case.rubrics
    }

    calibration_verdicts = _score_all(backend, calibration_cases, rubrics, case_rubrics)
    baseline_verdicts = _score_all(backend, baseline_cases, rubrics, case_rubrics)

    calibration = calibrate(
        calibration_verdicts,
        human_labels=human_labels(calibration_cases),
        rubrics=rubrics,
        phase=args.phase,
        case_rubrics=case_rubrics,
    )
    report = evaluate_run(
        baseline_verdicts,
        rubrics=rubrics,
        phase=args.phase,
        calibration=calibration,
        case_rubrics=case_rubrics,
    )

    if args.out is not None:
        args.out.write_text(
            "\n".join(
                verdict.model_dump_json() for verdict in calibration_verdicts + baseline_verdicts
            )
            + "\n",
            encoding="utf-8",
        )

    print(f"backend: {backend.name}")
    print(render(report))
    return report.exit_code


if __name__ == "__main__":
    sys.exit(main())
