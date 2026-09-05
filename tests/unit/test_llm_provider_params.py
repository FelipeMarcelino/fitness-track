"""What each provider accepts, and what the gateway must strip (spec 7.4).

The table of 7.4 is a list of asymmetries, and item 1 states the shape of the
rule exactly: the allowed-parameter map is keyed by **`(provider, model)`**, not
by provider. `reasoning_format` is the reason — it is invalid on the `gpt-oss`
family and valid on *other* Groq models, so a provider-wide rule would be wrong
in one direction and a model-wide one unmaintainable in the other.

These are the two failures the table warns about, as tests:

- `temperature` (and `top_p`) reaching the Anthropic path, where current-
  generation models reject them outright;
- `reasoning_format` reaching the `gpt-oss` family, where passing it is an
  error.

Nothing here is a model *choice* — that stays in `config/models.yaml` and is
resolved by role (invariant 4). A family token is a capability fact about a
provider's API, which 7.4 requires the gateway to absorb in code.

No SDK client is constructed: payload building is a pure function on each
adapter, which is what lets the shape be asserted without a network or a key.
"""

from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from fittrack.llm.providers.anthropic import AnthropicProvider
from fittrack.llm.providers.base import ProviderRequest, sanitize_params
from fittrack.llm.providers.groq import GroqProvider

GPT_OSS = "openai/gpt-oss-120b"
OTHER_GROQ = "llama-3.3-70b-versatile"
CLAUDE = "claude-haiku-4-5"


def request(model: str, **params: object) -> ProviderRequest:
    return ProviderRequest(
        model=model,
        messages=(SystemMessage(content="regras"), HumanMessage(content="oi")),
        params=dict(params),
        schema=None,
        timeout_s=30,
    )


# --------------------------------------------------------------------------- #
# The two rules of 7.4 item 1
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("rejected", ["temperature", "top_p"])
def test_sampling_parameters_never_reach_the_anthropic_path(rejected: str) -> None:
    """Current-generation Claude models reject them (spec 7.4, sampling row)."""
    clean = sanitize_params(provider="anthropic", model=CLAUDE, params={rejected: 0.3})

    assert rejected not in clean


def test_reasoning_format_never_reaches_the_gpt_oss_family() -> None:
    """Passing it is an error on gpt-oss (spec 7.4)."""
    clean = sanitize_params(provider="groq", model=GPT_OSS, params={"reasoning_format": "parsed"})

    assert "reasoning_format" not in clean


def test_reasoning_format_survives_on_another_groq_model() -> None:
    """The asymmetry item 1 names: this is why the key is (provider, model).

    A provider-wide rule would strip a parameter that is valid here, and the
    map would be quietly wrong rather than loudly wrong.
    """
    clean = sanitize_params(
        provider="groq", model=OTHER_GROQ, params={"reasoning_format": "parsed"}
    )

    assert clean["reasoning_format"] == "parsed"


def test_temperature_survives_on_the_groq_path() -> None:
    clean = sanitize_params(provider="groq", model=GPT_OSS, params={"temperature": 0.0})

    assert clean["temperature"] == 0.0


def test_sanitizing_invents_nothing() -> None:
    """It drops; it never adds. Shaping is each adapter's job, below."""
    params = {"temperature": 0.3, "reasoning_effort": "high"}

    assert sanitize_params(provider="groq", model=GPT_OSS, params=params) == params


def test_sanitizing_does_not_mutate_the_caller_s_mapping() -> None:
    params = {"temperature": 0.3}

    sanitize_params(provider="anthropic", model=CLAUDE, params=params)

    assert params == {"temperature": 0.3}


# --------------------------------------------------------------------------- #
# Groq: OpenAI-shaped payloads
# --------------------------------------------------------------------------- #


def test_the_groq_payload_carries_messages_in_openai_shape() -> None:
    payload = GroqProvider.build_payload(request(GPT_OSS, temperature=0.0))

    assert payload["model"] == GPT_OSS
    assert payload["messages"] == [
        {"role": "system", "content": "regras"},
        {"role": "user", "content": "oi"},
    ]
    assert payload["temperature"] == 0.0


def test_the_groq_payload_drops_what_gpt_oss_refuses() -> None:
    """The adapter sanitizes; a caller cannot route around the map."""
    payload = GroqProvider.build_payload(request(GPT_OSS, reasoning_format="parsed"))

    assert "reasoning_format" not in payload


def test_an_assistant_turn_keeps_its_role_on_the_groq_path() -> None:
    call = ProviderRequest(
        model=GPT_OSS,
        messages=(HumanMessage(content="oi"), AIMessage(content="olá")),
        params={},
        schema=None,
        timeout_s=30,
    )

    assert GroqProvider.build_payload(call)["messages"] == [
        {"role": "user", "content": "oi"},
        {"role": "assistant", "content": "olá"},
    ]


# --------------------------------------------------------------------------- #
# Anthropic: system is not a message, and effort is not `reasoning_effort`
# --------------------------------------------------------------------------- #


def test_the_anthropic_payload_lifts_system_out_of_the_message_list() -> None:
    """Anthropic takes the system prompt as its own argument, not as a turn."""
    payload = AnthropicProvider.build_payload(request(CLAUDE))

    assert payload["system"] == "regras"
    assert payload["messages"] == [{"role": "user", "content": "oi"}]


def test_the_anthropic_payload_never_carries_temperature() -> None:
    payload = AnthropicProvider.build_payload(request(CLAUDE, temperature=0.3))

    assert "temperature" not in payload


def test_reasoning_effort_becomes_the_anthropic_shape() -> None:
    """Spec 7.4, reasoning row: `thinking` plus `output_config.effort`.

    The role configuration speaks one vocabulary (`reasoning_effort`/`effort`)
    and each provider translates it. Passing the Groq spelling straight through
    would be a 400 the role config could not have predicted.
    """
    payload = AnthropicProvider.build_payload(request(CLAUDE, reasoning_effort="high"))

    assert payload["thinking"] == {"type": "adaptive"}
    assert payload["output_config"] == {"effort": "high"}
    assert "reasoning_effort" not in payload


def test_the_anthropic_payload_accepts_the_effort_spelling_too() -> None:
    """`models.yaml` uses `effort` on the Anthropic fallbacks and
    `reasoning_effort` on the Groq primaries; both mean the same thing here.
    """
    payload = AnthropicProvider.build_payload(request(CLAUDE, effort="high"))

    assert payload["output_config"] == {"effort": "high"}


def test_without_an_effort_the_anthropic_payload_asks_for_no_thinking() -> None:
    payload = AnthropicProvider.build_payload(request(CLAUDE))

    assert "thinking" not in payload
    assert "output_config" not in payload


def test_max_tokens_is_present_because_anthropic_requires_it() -> None:
    """The SDK refuses a request without it; a default here is not a policy."""
    payload = AnthropicProvider.build_payload(request(CLAUDE))

    assert payload["max_tokens"] > 0
