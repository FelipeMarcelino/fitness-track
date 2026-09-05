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

The last section covers the other half of what an adapter translates: a
**failure**. Spec 7.3 routes a 429 differently from an ordinary 400, and a
context-limit 400 differently from both — so a `ProviderError` carrying only an
exception class name would leave S03-T07 unable to implement that policy at
all.

No SDK client is constructed: payload building is a pure function on each
adapter, which is what lets the shape be asserted without a network or a key.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from pydantic import BaseModel

from fittrack.config import load_config
from fittrack.llm.providers.anthropic import AnthropicProvider
from fittrack.llm.providers.base import ProviderRequest, provider_failure, sanitize_params
from fittrack.llm.providers.groq import GroqProvider
from fittrack.llm.roles import LLMRole

# Resolved from `config/models.yaml`, never written here. Invariant 4 keeps
# model identifiers in configuration, and `test_no_model_name_appears_in_python`
# scans `src`, `evals` and `scripts` — not `tests`, which is exactly why these
# constants could have drifted into stale literals without anything noticing.
# Reading them from the file also means a model swap re-points these tests at
# what the deployment actually uses.
_EXTRACTOR = load_config(Path(__file__).resolve().parents[2] / "config").models.roles[
    LLMRole.EXTRACTOR
]
assert _EXTRACTOR.primary is not None
GPT_OSS = _EXTRACTOR.primary.model
CLAUDE = _EXTRACTOR.fallback.model

# Deliberately synthetic: the point of this one is to be a Groq model that is
# *not* in the `gpt-oss` family, and `models.yaml` has no such entry to read.
# A literal here names nothing real, so it cannot go stale.
OTHER_GROQ = "some-other-groq-model"


class Answer(BaseModel):
    """A schema to require, so the structured-output path has something to send."""

    reps: int


class FakeStatusError(Exception):
    """An SDK status error, in the three attributes the classifier reads.

    Both vendors' SDKs raise `APIStatusError` subclasses carrying
    `status_code` and the parsed `body`. The message is theirs and may quote
    the request back; the body's `code`/`type` are chosen by the provider.
    """

    def __init__(
        self, status_code: int, message: str, body: dict[str, object] | None = None
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.body = body


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

    assert payload["system"][0]["text"] == "regras"
    assert payload["messages"] == [{"role": "user", "content": "oi"}]


def test_the_anthropic_system_prompt_is_marked_cacheable() -> None:
    """Anthropic's prompt cache is explicit (spec 7.4, cache row).

    Groq caches automatically; Anthropic charges full price for every repeated
    prefix unless a block carries the breakpoint. The system prompt is the
    stable prefix of every call for a role — sending it as a bare string means
    `cache_read_input_tokens` can only ever report zero.
    """
    system = AnthropicProvider.build_payload(request(CLAUDE))["system"]

    assert system == [{"type": "text", "text": "regras", "cache_control": {"type": "ephemeral"}}]


def test_without_a_system_prompt_anthropic_gets_no_system_key() -> None:
    call = ProviderRequest(
        model=CLAUDE,
        messages=(HumanMessage(content="oi"),),
        params={},
        schema=None,
        timeout_s=30,
    )

    assert "system" not in AnthropicProvider.build_payload(call)


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
    """The SDK refuses a request without it; the default is a floor, not a policy."""
    payload = AnthropicProvider.build_payload(request(CLAUDE))

    assert payload["max_tokens"] > 0


def test_the_role_configuration_can_raise_the_output_budget() -> None:
    """A constant that no configuration can reach is a limit, not a default.

    It bites hardest on the high-effort fallbacks: adaptive thinking spends
    the same output budget the answer does, so a role pinned at the floor can
    run out of tokens reasoning and never write the answer.
    """
    payload = AnthropicProvider.build_payload(request(CLAUDE, max_tokens=32_000))

    assert payload["max_tokens"] == 32_000


def test_both_effort_spellings_leave_the_payload_when_both_are_present() -> None:
    """`a or b` short-circuits, and the second `pop` is what removes the alias.

    A partial override that sets `reasoning_effort` on a fallback that already
    inherits `effort` — the shape the shipped ANALYST and COACH fallbacks would
    take — leaves both after the merge. Selecting with `or` would then never
    run the second pop, and `effort` would ride along in `**params` as a
    top-level keyword Anthropic does not accept.
    """
    payload = AnthropicProvider.build_payload(
        request(CLAUDE, reasoning_effort="high", effort="low")
    )

    assert "effort" not in payload
    assert "reasoning_effort" not in payload
    assert payload["output_config"] == {"effort": "high"}


def test_effort_never_reaches_the_groq_path() -> None:
    """`effort` is Anthropic's spelling; Groq's is `reasoning_effort`.

    `ModelSpec` accepts both fields for either provider, so an agent override
    that merged `effort` onto a Groq primary would forward a keyword that
    provider's API does not have. The role config is not provider-discriminated,
    so the map is where this stops.
    """
    clean = sanitize_params(provider="groq", model=GPT_OSS, params={"effort": "high"})

    assert "effort" not in clean


def test_reasoning_effort_still_reaches_the_groq_path() -> None:
    """The spelling Groq does accept survives: dropping both would silence the
    reasoning tier of 7.2 on its own primary.
    """
    clean = sanitize_params(provider="groq", model=GPT_OSS, params={"reasoning_effort": "high"})

    assert clean["reasoning_effort"] == "high"


# --------------------------------------------------------------------------- #
# Anthropic structured output, and the prefill that is a guaranteed 400
# --------------------------------------------------------------------------- #


def structured(model: str, **params: object) -> ProviderRequest:
    return ProviderRequest(
        model=model,
        messages=(HumanMessage(content="oi"),),
        params=dict(params),
        schema=Answer,
        timeout_s=30,
    )


def test_the_anthropic_payload_carries_the_requested_schema() -> None:
    """Without this, every structured role that falls back returns prose.

    Anthropic is the fallback of every role in `models.yaml` (spec 7.2), so a
    payload that ignored `schema` would turn each fallback of a structured
    agent into a validation failure — defeating the one path that exists to
    rescue the call (spec 7.3).
    """
    payload = AnthropicProvider.build_payload(structured(CLAUDE))

    assert payload["output_config"]["format"] == {
        "type": "json_schema",
        "schema": Answer.model_json_schema(),
    }


def test_effort_and_schema_share_one_output_config() -> None:
    """`output_config` carries both keys; writing one must not erase the other."""
    output_config = AnthropicProvider.build_payload(structured(CLAUDE, effort="high"))[
        "output_config"
    ]

    assert output_config["effort"] == "high"
    assert output_config["format"]["type"] == "json_schema"


def test_a_trailing_assistant_turn_is_dropped_on_the_anthropic_path() -> None:
    """Spec 7.4: prefill is a 400 on the current models — "nunca usar".

    A history whose last turn is the bot's is ordinary, and Groq accepts it as
    a prefill. Forwarding it to Anthropic guarantees the failure, and doing it
    on the *fallback* path would defeat the fallback.
    """
    call = ProviderRequest(
        model=CLAUDE,
        messages=(HumanMessage(content="oi"), AIMessage(content="olá")),
        params={},
        schema=None,
        timeout_s=30,
    )

    assert AnthropicProvider.build_payload(call)["messages"] == [{"role": "user", "content": "oi"}]


def test_an_assistant_turn_in_the_middle_survives_on_the_anthropic_path() -> None:
    """Only a trailing one is prefill; the rest is conversation."""
    call = ProviderRequest(
        model=CLAUDE,
        messages=(
            HumanMessage(content="oi"),
            AIMessage(content="olá"),
            HumanMessage(content="supino"),
        ),
        params={},
        schema=None,
        timeout_s=30,
    )

    assert [turn["role"] for turn in AnthropicProvider.build_payload(call)["messages"]] == [
        "user",
        "assistant",
        "user",
    ]


def test_a_late_system_message_does_not_hide_a_trailing_assistant_turn() -> None:
    """Order between the two rules, and it is not arbitrary.

    With the system message last, a prefill check running before the lift
    finds a system turn at the end and strips nothing — then the lift removes
    that turn and the payload ends in `assistant` anyway. Both rules applied,
    the guaranteed 400 still produced.
    """
    call = ProviderRequest(
        model=CLAUDE,
        messages=(
            HumanMessage(content="oi"),
            AIMessage(content="olá"),
            SystemMessage(content="contexto tardio"),
        ),
        params={},
        schema=None,
        timeout_s=30,
    )

    payload = AnthropicProvider.build_payload(call)

    assert payload["messages"] == [{"role": "user", "content": "oi"}]
    assert payload["system"][0]["text"] == "contexto tardio"


def test_a_trailing_assistant_turn_survives_on_the_groq_path() -> None:
    """The asymmetry is the point: Groq supports prefill (spec 7.4)."""
    call = ProviderRequest(
        model=GPT_OSS,
        messages=(HumanMessage(content="oi"), AIMessage(content="olá")),
        params={},
        schema=None,
        timeout_s=30,
    )

    assert GroqProvider.build_payload(call)["messages"][-1]["role"] == "assistant"


# --------------------------------------------------------------------------- #
# A failure carries a category, never the provider's message (spec 7.3)
# --------------------------------------------------------------------------- #


def test_a_rate_limit_is_reported_with_its_status() -> None:
    """7.3 retries a 429 and refuses to fall back on an ordinary 400. The
    policy needs to tell them apart, and a class name cannot.
    """
    failure = provider_failure("groq", FakeStatusError(429, "slow down"))

    assert failure.status_code == 429
    assert failure.context_limit is False


def test_a_context_limit_400_is_flagged_apart_from_an_ordinary_400() -> None:
    """The one 400 another provider can resolve (spec 7.3, and the reason the
    section spells out why it is the exception).

    Both arrive as the SDK's bad-request error, so without the flag the
    fallback either never happens or happens for programming errors too.
    """
    too_long = FakeStatusError(400, "this request exceeds the model's limit")
    too_long.body = {"error": {"code": "context_length_exceeded"}}
    ordinary = FakeStatusError(400, "invalid schema for tool")
    ordinary.body = {"error": {"code": "invalid_request_error"}}

    over = provider_failure("groq", too_long)
    plain = provider_failure("groq", ordinary)

    assert (over.status_code, over.context_limit) == (400, True)
    assert (plain.status_code, plain.context_limit) == (400, False)


def test_a_connection_failure_has_no_status() -> None:
    """Timeouts and connection errors retry by 7.3 without ever having one."""
    failure = provider_failure("anthropic", ConnectionResetError("boom"))

    assert failure.status_code is None
    assert failure.context_limit is False


def test_user_text_quoting_a_marker_is_not_a_context_limit() -> None:
    """The classifier must not read attacker-influenced text.

    A provider's 400 quotes the request back, and the request is the user's
    message. An unrestricted substring scan would let a user write "context
    window" in a workout log and have their ordinary malformed-request 400
    routed to the fallback — which §7.3 permits only for a real context limit,
    and which would mask the programming error behind a paid retry.
    """
    error = FakeStatusError(400, 'invalid tool schema; request was: "meu context window de treino"')

    assert provider_failure("groq", error).context_limit is False


def test_a_structured_error_code_classifies_the_context_limit() -> None:
    """The authoritative signal: the vendor's own code, not its prose."""
    error = FakeStatusError(400, "whatever")
    error.body = {"error": {"code": "context_length_exceeded", "type": "invalid_request_error"}}

    assert provider_failure("groq", error).context_limit is True


def test_the_anthropic_message_is_matched_only_when_it_is_unmistakable() -> None:
    """Anthropic reports both 400s as `invalid_request_error`, so the code
    alone cannot separate them and the vendor's message field is the only
    signal left.

    Matched only at the start of that field, and only in a shape carrying
    token counts — the vendor writes its own sentence first and echoes the
    request afterwards, if at all.
    """
    over = FakeStatusError(
        400,
        "bad request",
        body={
            "type": "error",
            "error": {
                "type": "invalid_request_error",
                "message": "prompt is too long: 215838 tokens > 204798 maximum",
            },
        },
    )

    assert provider_failure("anthropic", over).context_limit is True


def test_user_text_shaped_like_a_vendor_message_is_not_a_context_limit() -> None:
    """The classifier never reads `str(error)`, which carries the whole body.

    An error with no parsed body is not an invitation to scan the exception
    text: that text is where the request — and so the user's own words — end
    up, and a message *shaped* like the vendor's would otherwise buy a paid
    fallback for what is really a programming error.
    """
    echoed = FakeStatusError(
        400, 'invalid tool call; request said "prompt is too long: 123 tokens"'
    )

    assert provider_failure("groq", echoed).context_limit is False


def test_an_echo_inside_the_vendor_message_field_is_not_matched_either() -> None:
    """Anchored at the start: the vendor's own sentence opens the field."""
    echoed = FakeStatusError(
        400,
        "bad request",
        body={"error": {"message": 'schema invalid for "prompt is too long: 99 tokens"'}},
    )

    assert provider_failure("groq", echoed).context_limit is False


# --------------------------------------------------------------------------- #
# Transport failures are retriable; a bug in our own code is not (spec 7.3)
# --------------------------------------------------------------------------- #


def test_a_timeout_is_reported_as_a_transport_failure() -> None:
    """7.3 retries timeout/connection and nothing else without a status.

    Without a structured flag, a client-side `TypeError` raised while building
    the payload looks exactly like a timeout — both statusless — and the
    policy would have to either retry programming errors or drop genuine
    transient failures.
    """
    failure = provider_failure("groq", TimeoutError("read timed out"))

    assert failure.transport is True
    assert failure.status_code is None


def test_a_connection_reset_is_a_transport_failure() -> None:
    assert provider_failure("anthropic", ConnectionResetError("boom")).transport is True


def test_a_bug_in_our_own_code_is_not_a_transport_failure() -> None:
    """A `TypeError` from building the payload must never be retried."""
    failure = provider_failure("groq", TypeError("build_payload() got an unexpected keyword"))

    assert failure.transport is False
    assert failure.kind == "TypeError"


def test_the_sdk_transport_errors_are_recognised_by_name() -> None:
    """Neither SDK's transport error subclasses a stdlib one."""

    class APITimeoutError(Exception):
        pass

    assert provider_failure("groq", APITimeoutError("slow")).transport is True


def test_a_failure_never_carries_the_provider_message() -> None:
    """A provider error quotes the request back, and the request is the user's
    text (invariant 10, spec 20.6). The category is the diagnosis; the body is
    not ours to propagate.
    """
    secret = "supino reto com 80kg, 8 reps"

    failure = provider_failure("groq", FakeStatusError(400, f"invalid request: {secret}"))

    assert secret not in str(failure)
    assert secret not in repr(failure)
