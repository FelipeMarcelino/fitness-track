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

import re
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
    "provider_failure",
    "sanitize_params",
]


class ProviderError(RuntimeError):
    """A provider could not answer, with the category the retry policy reads.

    Spec 7.3 routes four outcomes differently: 429/5xx/timeout/connection retry
    and then fall back; a context-limit 400 falls back *without* retrying,
    because it is the one 400 another provider's window can resolve; any other
    400 is a programming error and must not reach the fallback at all.

    Telling those apart needs more than an exception class name — both 400s
    arrive as the SDK's bad-request type. So the status travels, and whether it
    was a context-limit travels with it.

    The provider's *message* deliberately does not. It quotes the request back,
    and the request is the user's text (invariant 10, spec 20.6).
    """

    def __init__(
        self,
        vendor: str,
        *,
        status_code: int | None = None,
        context_limit: bool = False,
        transport: bool = False,
        kind: str = "error",
    ) -> None:
        detail = f"HTTP {status_code}" if status_code is not None else kind
        if context_limit:
            detail += ", context limit"
        super().__init__(f"{vendor} call failed ({detail})")
        self.vendor = vendor
        self.status_code = status_code
        self.context_limit = context_limit
        # Whether the call never reached a verdict: a timeout or a dropped
        # connection. 7.3 retries these and nothing else that lacks a status —
        # a `TypeError` raised while building the payload is statusless too,
        # and retrying it would repeat the bug on a schedule. Without the flag
        # the two are indistinguishable, and the policy would have to guess.
        self.transport = transport
        # The exception class, kept rather than only interpolated: it is the
        # residual diagnosis when there is no status, and it is safe — a type
        # name is ours or the SDK's, never the request.
        self.kind = kind


# The vendors' own machine-readable codes for "the prompt does not fit".
# These are the authoritative signal: a code is chosen by the provider, never
# echoed from the request.
_CONTEXT_LIMIT_CODES = frozenset(
    {
        "context_length_exceeded",
        "string_above_max_length",
        "request_too_large",
    }
)

# Anthropic reports a context limit and a malformed request under the same
# `invalid_request_error` type, so for that vendor the message is the only
# signal left — and the message is the part that quotes the request back.
#
# Anchored on the token counts the vendors emit rather than on a bare phrase.
# A user writing "meu context window de treino" in a workout log would match
# a loose substring scan, and §7.3 sends a context limit to the fallback while
# refusing to send an ordinary 400 there at all: a false positive turns a
# programming error into a paid retry that hides it.
# Anchored at the start of the vendor's message field, not searched anywhere
# inside it: a provider opens with its own sentence and echoes the request
# afterwards, if at all. `search` over the whole string would match the echo.
_CONTEXT_LIMIT_PATTERNS = (
    re.compile(r"^\s*prompt is too long:\s*\d+"),
    re.compile(r"^\s*(this )?(model's )?maximum context length is\s*\d+"),
    re.compile(r"^\s*\d+\s*tokens?\s*>\s*\d+"),
)

# The SDK transport errors, by class name. Neither vendor's subclasses a
# stdlib exception, and importing both SDKs here to `isinstance` against them
# would couple the port to the adapters it exists to keep apart.
_TRANSPORT_CLASSES = frozenset({"APITimeoutError", "APIConnectionError"})


def _is_transport(error: Exception) -> bool:
    """Whether the request never reached a verdict (spec 7.3's retry class)."""
    if isinstance(error, (TimeoutError, ConnectionError)):
        return True
    return type(error).__name__ in _TRANSPORT_CLASSES


def _error_body(error: Exception) -> Mapping[str, Any]:
    """The parsed error object both SDKs attach, or an empty mapping."""
    body = getattr(error, "body", None)
    if isinstance(body, Mapping):
        inner = body.get("error")
        return inner if isinstance(inner, Mapping) else body
    return {}


def _is_context_limit(error: Exception) -> bool:
    """Whether this is the one 400 another provider's window can resolve.

    Structured first, message second — never the message alone, and never the
    whole `str(error)`, which carries the request body.
    """
    body = _error_body(error)
    for key in ("code", "type"):
        value = body.get(key)
        if isinstance(value, str) and value.lower() in _CONTEXT_LIMIT_CODES:
            return True
    # Only the vendor's own message field, never `str(error)`. The latter
    # renders the whole body, which is where the echoed request — and so the
    # user's words — end up. An error with no parsed body gets no textual
    # classification at all: 7.3's preflight is the primary mechanism, and a
    # false positive here buys a paid fallback for a programming error, which
    # is the outcome 7.3 explicitly refuses.
    message = body.get("message")
    if not isinstance(message, str):
        return False
    return any(pattern.match(message.lower()) for pattern in _CONTEXT_LIMIT_PATTERNS)


def provider_failure(vendor: str, error: Exception) -> ProviderError:
    """Classify an SDK exception into the categories 7.3 routes on.

    Reads the error to set the flag and then drops everything but the
    classification, which is the only part safe to carry forward.
    """
    status_code = getattr(error, "status_code", None)
    if not isinstance(status_code, int):
        status_code = None
    return ProviderError(
        vendor,
        status_code=status_code,
        context_limit=_is_context_limit(error),
        transport=_is_transport(error),
        kind=type(error).__name__,
    )


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
    # Anthropic reports a cache *write* apart from both the ordinary input
    # count and the cached read (spec 7.4, cache row). Groq caches
    # automatically and reports no such thing, so this stays zero there.
    cache_creation_tokens: int = 0


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

# `effort` is Anthropic's spelling of the reasoning knob; Groq's is
# `reasoning_effort`, and its API has no `effort` at all. `ModelSpec` accepts
# both fields for either provider — `models.yaml` uses one on the primaries and
# the other on the fallbacks — so an agent override that merged the wrong one
# onto a Groq spec would forward a keyword that provider does not have.
# Provider-wide, unlike the rule below, because no Groq model takes it.
_GROQ_REJECTS = frozenset({"effort"})

# Valid on other Groq models; an error on this family (spec 7.4).
_GPT_OSS_REJECTS = frozenset({"reasoning_format"})


def _rejected_by(provider: Provider, model: str) -> frozenset[str]:
    if provider == "anthropic":
        return _ANTHROPIC_REJECTS
    if provider == "groq":
        if GPT_OSS_FAMILY in model:
            return _GROQ_REJECTS | _GPT_OSS_REJECTS
        return _GROQ_REJECTS
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
