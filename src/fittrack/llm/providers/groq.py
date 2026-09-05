"""Groq, on the native SDK (ADR-0011).

Two things in spec 7.4 shape this file:

- `reasoning_format` is an error on the `gpt-oss` family, so the payload is
  built through `sanitize_params` rather than from the role config directly —
  the adapter cannot be routed around.
- `strict: true` on structured output demands more than Pydantic emits (item 2:
  every field `required`, `additionalProperties: false`), and the schemas of 9.4
  have optionals with defaults. So the request asks for the schema *without*
  `strict`, and the gateway revalidates with Pydantic — which item 5 requires in
  both directions anyway. Best-effort here costs a retry on a malformed answer;
  a transformed schema would cost a second definition of every contract.
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

__all__ = ["GroqProvider"]

# `BaseMessage.type` to the role name the OpenAI-shaped API expects.
_ROLES = {"system": "system", "human": "user", "ai": "assistant", "tool": "tool"}


class GroqProvider:
    """The primary provider of ADR-0001, as a port implementation."""

    name: ClassVar[Provider] = "groq"

    def __init__(self, client: Any) -> None:
        # Injected: the key lives in settings and the pool is the process's.
        # An adapter that built its own client would make every test that
        # imports this module need a credential.
        self._client = client

    @staticmethod
    def build_payload(request: ProviderRequest) -> dict[str, Any]:
        """The request body, as a plain mapping.

        Pure and static on purpose: the shape of the payload is the part of
        this adapter worth asserting, and a test should not need a client, a
        key or a socket to assert it.
        """
        payload: dict[str, Any] = {
            "model": request.model,
            "messages": [
                {"role": _ROLES.get(message.type, "user"), "content": message.content}
                for message in request.messages
            ],
            **sanitize_params(provider="groq", model=request.model, params=request.params),
        }
        if request.schema is not None:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": request.schema.__name__,
                    "schema": request.schema.model_json_schema(),
                },
            }
        return payload

    async def complete(self, request: ProviderRequest) -> ProviderResponse:
        try:
            answer = await self._client.chat.completions.create(
                **self.build_payload(request), timeout=request.timeout_s
            )
        except Exception as error:
            # The type, never the message: a provider error can quote the
            # request body back, and the body is the user's text (spec 20.6).
            raise ProviderError(f"groq call failed: {type(error).__name__}") from None

        text = answer.choices[0].message.content or ""
        usage = getattr(answer, "usage", None)
        details = getattr(usage, "prompt_tokens_details", None)
        return ProviderResponse(
            text=text,
            input_tokens=getattr(usage, "prompt_tokens", 0) or 0,
            output_tokens=getattr(usage, "completion_tokens", 0) or 0,
            cached_tokens=getattr(details, "cached_tokens", 0) or 0,
        )
