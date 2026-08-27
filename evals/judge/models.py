"""Typed shapes of a judge case and a judge verdict."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

Kind = Literal["analysis", "recommendation", "program", "persona", "ingestion"]
Label = Literal["good", "bad"]


class ToolResult(BaseModel):
    """A deterministic SQL tool result (section 16); the ground truth for numbers."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tool: str
    args: dict[str, Any] = Field(default_factory=dict)
    result: dict[str, Any]


class JudgeCase(BaseModel):
    """One agent answer to be scored, with everything the judge needs to score it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    kind: Kind
    user_message: str
    response: str
    profile: dict[str, Any] = Field(default_factory=dict)
    context: dict[str, Any] = Field(default_factory=dict)
    tool_results: list[ToolResult] = Field(default_factory=list)
    retrieved: list[str] = Field(default_factory=list)
    rubrics: list[str] = Field(default_factory=list)
    label: Label | None = None
    human_scores: dict[str, int] = Field(default_factory=dict)
    notes: str = ""


class RubricScore(BaseModel):
    # Strict on purpose: the provider schema asks for an integer, and a lax
    # `int(5.9)` would turn a schema-invalid answer into a passing 5 on a
    # blocking rubric.
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    rubric: str
    score: int = Field(ge=1, le=5)
    justification: str


class CaseVerdict(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    case_id: str
    scores: dict[str, RubricScore]

    def score_of(self, rubric: str) -> int | None:
        found = self.scores.get(rubric)
        return None if found is None else found.score
