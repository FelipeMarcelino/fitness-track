"""Versioned judge datasets."""

from __future__ import annotations

import json
from pathlib import Path

from evals.judge.models import JudgeCase

DATASETS_DIR = Path(__file__).resolve().parent.parent / "datasets"
CALIBRATION = DATASETS_DIR / "judge_calibration.jsonl"
BASELINE = DATASETS_DIR / "judge_baseline.jsonl"


def load_cases(path: Path) -> list[JudgeCase]:
    """Read a JSONL dataset, validating every record against `JudgeCase`."""
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
    return cases
