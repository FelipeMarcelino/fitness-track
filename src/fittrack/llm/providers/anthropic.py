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

from fittrack.config import Provider
from fittrack.llm.providers.base import (
    ProviderError,
    ProviderRequest,
    ProviderResponse,
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

_ROLES = {"human": "user", "ai": "assistant", "tool": "user"}


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
        # Anthropic fallbacks.
        effort = params.pop("reasoning_effort", None) or params.pop("effort", None)

        system = " ".join(
            str(message.content) for message in request.messages if message.type == "system"
        )
        payload: dict[str, Any] = {
            "model": request.model,
            "max_tokens": params.pop("max_tokens", DEFAULT_MAX_TOKENS),
            "messages": [
                {"role": _ROLES.get(message.type, "user"), "content": message.content}
                for message in request.messages
                if message.type != "system"
            ],
            **params,
        }
        if system:
            payload["system"] = system
        if effort:
            payload["thinking"] = dict(_THINKING)
            payload["output_config"] = {"effort": effort}
        return payload

    async def complete(self, request: ProviderRequest) -> ProviderResponse:
        try:
            answer = await self._client.messages.create(
                **self.build_payload(request), timeout=request.timeout_s
            )
        except Exception as error:
            raise ProviderError(f"anthropic call failed: {type(error).__name__}") from None

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
        )
