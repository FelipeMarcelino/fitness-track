"""The dataset format (§21.1).

JSONL, one case per line, because the golden set grows from real failures
(§21.5): appending a line is something an operator can do to a file, and a diff
of one added line is reviewable in a way a rewritten JSON array is not.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Case:
    """One evaluation example."""

    id: str
    input: str
    expected: dict[str, Any]
    tags: list[str] = field(default_factory=list)


def load_cases(path: Path) -> list[Case]:
    """Reads a JSONL dataset.

    A missing file raises rather than returning nothing. The failure this
    guards against is quiet: a renamed dataset that yields zero cases scores a
    perfect zero-of-zero, and the suite goes green over an empty run.

    A malformed line raises with its line number, because "invalid JSON" in a
    file of three hundred lines is not an actionable error message.
    """
    if not path.exists():
        raise FileNotFoundError(f"dataset not found: {path}")

    cases: list[Case] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            row = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path}: line {number} is not valid JSON: {exc}") from exc
        cases.append(_case(row, path, number))
    return cases


def _case(row: object, path: Path, number: int) -> Case:
    """Validates one row and names its line when it is wrong.

    Every check here guards a way a malformed dataset scores well. The worst is
    a missing or misspelt `expected`: the case then contributes no fields, and
    a suite that evaluated nothing reports every floor as met.
    """

    def bad(reason: str) -> ValueError:
        return ValueError(f"{path}: line {number} {reason}")

    if not isinstance(row, dict):
        raise bad(f"is a {type(row).__name__}, not an object")
    for key in ("id", "input", "expected"):
        if key not in row:
            raise bad(f"has no {key!r}")
    expected = row["expected"]
    if not isinstance(expected, dict):
        # `null` lands here too, which is the point: a row with nothing to
        # check should not silently become a row that passes.
        raise bad(f"has a {type(expected).__name__} for 'expected', not an object")
    tags = row.get("tags") or []
    if not isinstance(tags, list):
        raise bad(f"has a {type(tags).__name__} for 'tags', not a list")

    return Case(
        id=str(row["id"]),
        input=str(row["input"]),
        expected=dict(expected),
        tags=[str(tag) for tag in tags],
    )
