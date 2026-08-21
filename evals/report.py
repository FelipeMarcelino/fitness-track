"""How a run reports itself (§21.4)."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class SuiteResult:
    name: str
    passed: bool
    total: int
    summary: str
    failures: list[str] = field(default_factory=list)


def exit_code(results: list[SuiteResult]) -> int:
    """Non-zero if any suite failed.

    Note what is not here: an uncalibrated judge produces a *passing* result
    (§21.2). Discarding a round is not the same as failing a PR, and conflating
    them would block a change for something that changed under the judge.
    """
    return 0 if all(result.passed for result in results) else 1


def render(results: list[SuiteResult]) -> str:
    lines = []
    for result in results:
        mark = "PASS" if result.passed else "FAIL"
        lines.append(f"[{mark}] {result.name}: {result.summary}")
        lines.extend(f"       - {failure}" for failure in result.failures[:20])
        if len(result.failures) > 20:
            lines.append(f"       ... and {len(result.failures) - 20} more")
    return "\n".join(lines)
