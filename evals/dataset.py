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
        cases.append(
            Case(
                id=str(row["id"]),
                input=str(row["input"]),
                expected=dict(row.get("expected") or {}),
                tags=list(row.get("tags") or []),
            )
        )
    return cases
