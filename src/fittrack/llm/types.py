"""What crosses the gateway boundary (spec 7.1).

Two rules shape every type here.

**Nothing carries a model name the caller chose.** `LLMResult` reports which
model answered, because `usage_ledger` records it (spec 5.2) — but the caller
named an agent and a role, and the identifier came from `models.yaml`
(invariant 4).

**Nothing prints user content.** The prompt and the answer are the two most
sensitive strings the system handles, and a repr is where they would reach a
traceback, a log line, or Datadog — none of which may have them (invariant 10,
spec 20.6). Langfuse gets them deliberately, through the tracing of S03-T07;
`field(repr=False)` is what keeps every other channel from getting them by
accident.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from pydantic import BaseModel

from fittrack.config import Provider
from fittrack.llm.roles import LLMRole

__all__ = ["LLMResult", "TokenUsage"]


@dataclass(frozen=True, slots=True)
class TokenUsage:
    """What the call consumed, in the shape `usage_ledger` stores (spec 5.2).

    Zero rather than `None` for the counts a provider does not report: the
    ledger's columns are `NOT NULL DEFAULT 0`, and a missing count is an
    absence of billing information, not an unknown quantity to propagate.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0


@dataclass(frozen=True, slots=True)
class LLMResult:
    """One answered invocation.

    `agent` and `role` travel back with the answer because every metric of 20.3
    is labelled by agent and every ledger row records one — recovering them
    from the call site later would mean trusting two places to agree.
    """

    # The answer itself. Never in a repr: see the module docstring.
    text: str = field(repr=False)
    # `None` when the caller asked for no schema — `voice` writes prose (9.7).
    parsed: BaseModel | None = field(repr=False)
    provider: Provider
    model: str
    usage: TokenUsage
    agent: str
    role: LLMRole
