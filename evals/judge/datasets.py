"""Versioned judge datasets."""

from __future__ import annotations

import json
from collections.abc import Collection
from pathlib import Path

from evals.judge.models import JudgeCase

DATASETS_DIR = Path(__file__).resolve().parent.parent / "datasets"
CALIBRATION = DATASETS_DIR / "judge_calibration.jsonl"
BASELINE = DATASETS_DIR / "judge_baseline.jsonl"


def load_cases(path: Path, known_rubrics: Collection[str] | None = None) -> list[JudgeCase]:
    """Read a JSONL dataset, validating every record against `JudgeCase`.

    Pass `known_rubrics` to reject a misspelled declaration outright. Narrowing
    silently drops a name it does not recognise, which for a blocking rubric
    means the gate quietly stops being a gate.
    """
    cases: list[JudgeCase] = []
    with path.open(encoding="utf-8") as handle:
        for number, line in enumerate(handle, start=1):
            stripped = line.strip()
            if not stripped or stripped.startswith("//"):
                continue
            try:
                cases.append(JudgeCase.model_validate(json.loads(stripped)))
            except (json.JSONDecodeError, ValueError) as error:
                raise ValueError(f"{path.name}:{number}: {error}") from error
            if known_rubrics is not None:
                unknown = sorted(set(cases[-1].rubrics) - set(known_rubrics))
                if unknown:
                    raise ValueError(
                        f"{path.name}:{number}: case {cases[-1].id!r} declares "
                        f"unknown rubric(s): {', '.join(unknown)}"
                    )
    return cases
