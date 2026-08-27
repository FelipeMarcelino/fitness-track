"""Loading and typing of the rubric set in `evals/rubrics/`."""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

RUBRICS_DIR = Path(__file__).resolve().parent.parent / "rubrics"

_FRONTMATTER = re.compile(r"\A---\n(?P<meta>.*?)\n---\n(?P<body>.*)\Z", re.DOTALL)
_SCALE_ITEM = re.compile(r"^(?P<level>[1-5])\.\s+(?P<text>.+)$", re.MULTILINE)


class RubricError(ValueError):
    """A rubric file does not satisfy the contract of section 21.2."""


@dataclass(frozen=True, slots=True)
class Rubric:
    """One scoring dimension of the judge."""

    id: str
    title: str
    criterion: str
    scale: dict[int, str]
    blocking: bool
    min_score: int
    since_phase: str
    # A universal rubric applies to every answer, and no case may declare its
    # way out of it. A blocking rubric that is *not* universal needs material a
    # case may simply not have — `channel_equivalence` needs two paired outputs.
    universal: bool

    def fails(self, score: int) -> bool:
        return score < self.min_score


def _parse(path: Path) -> Rubric:
    match = _FRONTMATTER.match(path.read_text(encoding="utf-8"))
    if match is None:
        raise RubricError(f"{path.name}: missing YAML frontmatter")

    meta: Any = yaml.safe_load(match["meta"])
    if not isinstance(meta, dict):
        raise RubricError(f"{path.name}: frontmatter is not a mapping")

    missing = {"id", "title", "blocking", "min_score", "since_phase", "universal"} - set(meta)
    if missing:
        raise RubricError(f"{path.name}: missing frontmatter keys {sorted(missing)}")
    if meta["id"] != path.stem:
        raise RubricError(f"{path.name}: id {meta['id']!r} does not match the file name")

    body = match["body"]
    criterion, _, scale_body = body.partition("## Escala")
    criterion = criterion.replace("## Critério", "").strip()
    if not criterion:
        raise RubricError(f"{path.name}: empty criterion")

    scale = {int(m["level"]): m["text"].strip() for m in _SCALE_ITEM.finditer(scale_body)}
    if set(scale) != {1, 2, 3, 4, 5}:
        raise RubricError(f"{path.name}: the scale must define levels 1 through 5")

    blocking = bool(meta["blocking"])
    min_score = int(meta["min_score"])
    if blocking and min_score != 5:
        # Spec 21.2: a blocking rubric fails on "qualquer caso < 5".
        raise RubricError(f"{path.name}: a blocking rubric must require a score of 5")
    if bool(meta["universal"]) and not blocking:
        raise RubricError(f"{path.name}: a universal rubric must also be blocking")

    return Rubric(
        id=str(meta["id"]),
        title=str(meta["title"]),
        criterion=criterion,
        scale=scale,
        blocking=blocking,
        min_score=min_score,
        since_phase=str(meta["since_phase"]),
        universal=bool(meta["universal"]),
    )


@lru_cache(maxsize=1)
def load_rubrics(directory: Path | None = None) -> dict[str, Rubric]:
    """Every rubric on disk, keyed by id."""
    base = directory or RUBRICS_DIR
    files = [p for p in sorted(base.glob("*.md")) if p.stem != "README"]
    rubrics = {r.id: r for r in (_parse(p) for p in files)}
    if not rubrics:
        raise RubricError(f"no rubric found in {base}")
    return rubrics


def _phase_key(phase: str) -> tuple[int, ...]:
    return tuple(int(part) for part in phase.split("."))


def active_rubrics(phase: str, rubrics: dict[str, Rubric] | None = None) -> dict[str, Rubric]:
    """The rubrics that apply at a given roadmap phase (section 24)."""
    current = _phase_key(phase)
    return {
        name: rubric
        for name, rubric in (rubrics or load_rubrics()).items()
        if _phase_key(rubric.since_phase) <= current
    }
