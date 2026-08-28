"""Roles and agent names (spec sections 7.2 and 7.2.1).

The unit of model configuration is the **role**, not the agent. Ten roles cover
about twenty agents, and several share one: `EXTRACTOR` serves `extraction` and
`correction`; `ROUTER` serves `router`, `clarification` and `onboarding`.

That is deliberate. A role is a class of cost and capability — "this has to
reason", "this is cheap classification at very high volume" — and it is the axis
on which the model decision is actually taken. Twenty configuration keys would
be eighteen duplicates and two differences, and the odds of two drifting apart
by inattention rather than by decision are high.
"""

from __future__ import annotations

from enum import StrEnum


class LLMRole(StrEnum):
    """A class of cost and capability, not an agent."""

    # Fast tier: very high volume, cheap classification and transformation.
    NORMALIZER = "NORMALIZER"
    ROUTER = "ROUTER"
    EXTRACTOR = "EXTRACTOR"
    RESOLVER = "RESOLVER"
    VOICE = "VOICE"
    GUARDRAIL = "GUARDRAIL"
    SUMMARY = "SUMMARY"

    # Reasoning tier: low volume, high cost per call.
    ANALYST = "ANALYST"
    COACH = "COACH"

    # Offline. No primary by design (section 7.2).
    JUDGE = "JUDGE"


# Every agent that may legitimately appear under `agents:` in models.yaml,
# mapped to the role it actually invokes (spec 9.2 and 9.5-9.8). The mapping,
# not just the names: an override that declares the wrong role would otherwise
# validate against itself and fail on the agent's first call instead of at boot.
#
# The deterministic nodes of section 9.2 are absent on purpose. `gamification`,
# for one, is pure SQL and invokes no LLM role, so an override for it would pass
# validation and do nothing — precisely the condition this registry rejects.
AGENT_ROLES: dict[str, LLMRole] = {
    "normalizer": LLMRole.NORMALIZER,
    "router": LLMRole.ROUTER,
    "clarification": LLMRole.ROUTER,
    "onboarding": LLMRole.ROUTER,
    "guardrail": LLMRole.GUARDRAIL,
    "extraction": LLMRole.EXTRACTOR,
    "correction": LLMRole.EXTRACTOR,
    "resolver": LLMRole.RESOLVER,
    "voice": LLMRole.VOICE,
    "summary": LLMRole.SUMMARY,
    "analysis": LLMRole.ANALYST,
    "volume_auditor": LLMRole.ANALYST,
    "recommendation": LLMRole.COACH,
    "program": LLMRole.COACH,
    "progression": LLMRole.COACH,
    "proactive": LLMRole.COACH,
}

KNOWN_AGENTS: frozenset[str] = frozenset(AGENT_ROLES)
