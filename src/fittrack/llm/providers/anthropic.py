"""Anthropic, on the native SDK (ADR-0011) — the product fallback of ADR-0001.

Three asymmetries from spec 7.4 live here, and each is a 400 if ignored:

- **The system prompt is not a turn.** It is its own argument, so it is lifted
  out of the message list rather than sent with `role: "system"`.
- **Sampling is rejected.** `temperature` and `top_p` are dropped by
  `sanitize_params` before the payload is built.
- **Reasoning has a different spelling.** The role config says
  `reasoning_effort` (or `effort` on the fallback specs); Anthropic wants
  `thinking` plus `output_config.effort`. Passing the config's spelling through
  would be a failure the role configuration could not have predicted.
"""

from __future__ import annotations

from typing import Any, ClassVar

from langchain_core.messages import BaseMessage

from fittrack.config import Provider
from fittrack.llm.providers.base import (
    ProviderRequest,
    ProviderResponse,
    provider_failure,
    sanitize_params,
)

__all__ = ["AnthropicProvider"]

# The SDK refuses a request without it, so it is a floor rather than a policy:
# no role in `models.yaml` declares an output budget, and inventing a
# per-role one here would put a limit in code that the configuration cannot
# see. A role that needs a larger answer gets the field in `models.yaml`.
DEFAULT_MAX_TOKENS = 4096

# Spec 7.4, reasoning row. Adaptive rather than a fixed budget: the effort
# level is what the role configuration expresses, and the two together are
# what the API reads.
_THINKING = {"type": "adaptive"}

# Spec 7.4, cache row: Anthropic's prompt cache is explicit, unlike Groq's.
# `ephemeral` is the only lifetime the API offers today.
_CACHE_BREAKPOINT = {"type": "ephemeral"}

_ROLES = {"human": "user", "ai": "assistant", "tool": "user"}


def _conversation(messages: tuple[BaseMessage, ...]) -> tuple[BaseMessage, ...]:
    """The turns Anthropic will see, with no trailing assistant among them.

    The system messages are removed **first**, and the order matters: a
    sequence ending `[human, ai, system]` has a system turn last, so a prefill
    check running before the lift would find nothing to strip — and then the
    lift would remove the system turn and leave the payload ending in
    `assistant` anyway, which is the 400 this exists to prevent.
    """
    return _without_prefill(tuple(m for m in messages if m.type != "system"))


def _without_prefill(messages: tuple[BaseMessage, ...]) -> tuple[BaseMessage, ...]:
    """Drop trailing assistant turns, which Anthropic reads as a prefill.

    Spec 7.4 is blunt about it: prefill is a 400 on the current models, "nunca
    usar". But a history whose last turn is the bot's is ordinary — Groq
    accepts exactly that, and the graph's `messages` (spec 8.2) can well end
    with an `AIMessage`. The provider cannot tell an accidental trailing turn
    from a deliberate prefill, so this path removes it.

    Dropping rather than raising: this adapter is every role's *fallback*
    (7.2), and refusing the call would defeat the one mechanism 7.3 has to
    rescue a failed primary — over a turn the model was about to regenerate
    anyway.
    """
    kept = list(messages)
    while kept and kept[-1].type == "ai":
        kept.pop()
    return tuple(kept)


class AnthropicProvider:
    """The cross-provider fallback: a different vendor, not a second model."""

    name: ClassVar[Provider] = "anthropic"

    def __init__(self, client: Any) -> None:
        self._client = client

    @staticmethod
    def build_payload(request: ProviderRequest) -> dict[str, Any]:
        """The request body, as a plain mapping. Pure — see `GroqProvider`."""
        params = sanitize_params(provider="anthropic", model=request.model, params=request.params)
        # Both spellings mean the same thing to a role: `models.yaml` uses
        # `reasoning_effort` on the Groq primaries and `effort` on the
        # Anthropic fallbacks. Both are popped *before* one is selected — with
        # `a or b` the second pop never runs when the first returns a value, so
        # a spec carrying both (a partial override merged onto a fallback that
        # already declares `effort`) would leave the alias in `params` and
        # expand it into the payload as a keyword Anthropic does not accept.
        by_groq_name = params.pop("reasoning_effort", None)
        by_anthropic_name = params.pop("effort", None)
        effort = by_groq_name or by_anthropic_name

        system = " ".join(
            str(message.content) for message in request.messages if message.type == "system"
        )
        payload: dict[str, Any] = {
            "model": request.model,
            "max_tokens": params.pop("max_tokens", DEFAULT_MAX_TOKENS),
            "messages": [
                {"role": _ROLES.get(message.type, "user"), "content": message.content}
                for message in _conversation(request.messages)
            ],
            **params,
        }
        if system:
            # A content block with an explicit breakpoint, not a bare string.
            # Groq caches repeated prefixes automatically; Anthropic charges
            # full price for every one unless a block carries `cache_control`
            # (spec 7.4, cache row). The system prompt is the stable prefix of
            # every call for a role, so sending it unmarked means
            # `cache_read_input_tokens` can only ever report zero.
            payload["system"] = [
                {"type": "text", "text": system, "cache_control": dict(_CACHE_BREAKPOINT)}
            ]
        if effort:
            payload["thinking"] = dict(_THINKING)
        # One object carries both halves (`OutputConfigParam`), so the schema
        # and the effort cannot be written independently without the second
        # erasing the first.
        output_config: dict[str, Any] = {}
        if effort:
            output_config["effort"] = effort
        if request.schema is not None:
            # Anthropic's own structured-output format (spec 7.4). Without it,
            # a structured role that falls back here gets prose, and the
            # gateway rejects the very answer the fallback existed to produce.
            output_config["format"] = {
                "type": "json_schema",
                "schema": request.schema.model_json_schema(),
            }
        if output_config:
            payload["output_config"] = output_config
        return payload

    async def complete(self, request: ProviderRequest) -> ProviderResponse:
        try:
            answer = await self._client.messages.create(
                **self.build_payload(request), timeout=request.timeout_s
            )
        except Exception as error:
            raise provider_failure("anthropic", error) from None

        # The first *text* block, not the first block: with `thinking` on, the
        # reasoning block comes first and is not the answer.
        text = next(
            (
                block.text
                for block in getattr(answer, "content", [])
                if getattr(block, "type", None) == "text"
            ),
            "",
        )
        usage = getattr(answer, "usage", None)
        return ProviderResponse(
            text=text,
            input_tokens=getattr(usage, "input_tokens", 0) or 0,
            output_tokens=getattr(usage, "output_tokens", 0) or 0,
            cached_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
            # The write side of the breakpoint above. Dropping it would let a
            # cache-creation call — the expensive one — report only its small
            # uncached suffix to the ledger of S03-T07.
            cache_creation_tokens=getattr(usage, "cache_creation_input_tokens", 0) or 0,
        )
