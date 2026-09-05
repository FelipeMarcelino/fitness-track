"""The provider port, and the parameter map of spec 7.4.

ADR-0011: the adapters are written on the **native** SDKs. `BaseMessage` from
`langchain-core` stays as the input vocabulary because the graph state of 8.2
already speaks it, but no provider object crosses out of this package.

The map below is the whole reason this module exists. Spec 7.4 item 1 states
the shape of the rule and the reason for it: the set of accepted parameters is
keyed by **`(provider, model)`**, because `reasoning_format` is an error on the
`gpt-oss` family and valid on other Groq models. A provider-wide rule would be
wrong in one direction; a per-model one would need a new entry for every model
id in `models.yaml`, which is exactly the coupling invariant 4 removes.

A family token is not a model choice. The choice lives in `config/models.yaml`
and is resolved by role; this is a statement about what a provider's API
accepts, which the gateway has to know to build a legal request at all.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, ClassVar, Protocol, runtime_checkable

from langchain_core.messages import BaseMessage
from pydantic import BaseModel

from fittrack.config import Provider

__all__ = [
    "LLMProvider",
    "ProviderError",
    "ProviderRequest",
    "ProviderResponse",
    "sanitize_params",
]


class ProviderError(RuntimeError):
    """A provider could not answer. S03-T07 classifies these for retry."""


@dataclass(frozen=True, slots=True)
class ProviderRequest:
    """One resolved call, as the adapter receives it.

    The model is already chosen: the gateway resolved `(agent, role)` through
    `models.yaml` before building this. An adapter never picks a model.
    """

    model: str
    # Never in a repr: this is the user's text and the system prompt
    # (invariant 10, spec 20.6).
    messages: tuple[BaseMessage, ...] = field(repr=False)
    params: Mapping[str, Any]
    schema: type[BaseModel] | None
    timeout_s: int


@dataclass(frozen=True, slots=True)
class ProviderResponse:
    """What an adapter reports back, before Pydantic gets a say.

    `text` is the raw body. Validation happens in the gateway, for every
    provider, whether or not the provider promised structured output — the
    validation is the source of truth (spec 7.4 item 5).
    """

    text: str = field(repr=False)
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0


@runtime_checkable
class LLMProvider(Protocol):
    """What the gateway needs from a provider, and nothing else."""

    name: ClassVar[Provider]

    async def complete(self, request: ProviderRequest) -> ProviderResponse:
        """Answer one request, or raise `ProviderError`."""


# The `gpt-oss` family, by the token that identifies it inside a model id.
# Deliberately not a full identifier: this is a capability rule, and writing
# the shipped model name here would put a model id in Python (invariant 4).
GPT_OSS_FAMILY = "gpt-oss"

# Sampling is rejected outright by current-generation Anthropic models
# (spec 7.4, sampling row), so the gateway removes rather than forwards.
_ANTHROPIC_REJECTS = frozenset({"temperature", "top_p"})

# Valid on other Groq models; an error on this family (spec 7.4).
_GPT_OSS_REJECTS = frozenset({"reasoning_format"})


def _rejected_by(provider: Provider, model: str) -> frozenset[str]:
    if provider == "anthropic":
        return _ANTHROPIC_REJECTS
    if provider == "groq" and GPT_OSS_FAMILY in model:
        return _GPT_OSS_REJECTS
    return frozenset()


def sanitize_params(*, provider: Provider, model: str, params: Mapping[str, Any]) -> dict[str, Any]:
    """The parameters this `(provider, model)` will accept, and no others.

    Drops; never adds. Turning `reasoning_effort` into whatever a provider
    calls it is the adapter's job — a rule that both removed and invented keys
    would be two decisions wearing one name, and the second is provider-shaped
    while this one is not.

    Returns a new mapping: the caller's `params` comes from a frozen
    `ModelSpec` and is shared across every call for that role.
    """
    rejected = _rejected_by(provider, model)
    return {name: value for name, value in params.items() if name not in rejected}
