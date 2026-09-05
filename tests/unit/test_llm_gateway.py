"""The single invocation contract for every LLM call (spec 7.1; sprint S03-T06).

Four properties carry this file, and none of them is "the SDK works":

1. **The caller names an agent and a role, never a model.** Invariant 4 lives or
   dies here: `ainvoke` resolves `(agent, role)` through
   `ModelsConfig.resolve()`, and a caller that wanted a specific model would
   have no way to ask for one.
2. **Pydantic is the source of truth, not the provider's promise.** Every
   response is revalidated even when the provider claims structured output
   (spec 7.4 item 5) — a field the schema does not declare is a failure, not a
   silently-kept extra.
3. **`tenant_id` is an argument, never something the model can influence**
   (invariant 3).
4. **Refusals happen at the contract, not at the provider.** A role with no
   primary (the JUDGE, deliberately — spec 7.2) fails by name here rather than
   as an `AttributeError` one layer down.

No test opens a socket: the provider is a port, and these hand it a fake that
records what it was asked for. That is also what keeps the retry, fallback,
quota and ledger of S03-T07 out of this file — they are not in this contract
yet, and a fake that never fails cannot pretend otherwise.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar, Literal

import pytest
from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage
from pydantic import BaseModel, field_validator

from fittrack.config import ModelsConfig, Provider, load_config
from fittrack.llm.gateway import LLMGateway, RoleHasNoPrimaryError
from fittrack.llm.providers.base import ProviderRequest, ProviderResponse
from fittrack.llm.roles import AGENT_ROLES, LLMRole
from fittrack.llm.types import LLMResult

CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"


def committed_models() -> ModelsConfig:
    """The configuration the repository actually ships.

    Deliberately not a fixture built in the test: the assertion that matters is
    that the gateway resolves what `config/models.yaml` declares, and a
    hand-built config would be asserting the test against itself.
    """
    return load_config(CONFIG_DIR).models


@dataclass
class FakeProvider:
    """A provider port that records the request instead of making one."""

    # `Provider`, not `str`: the port types it as the closed set of vendors,
    # and a fake that widened it would let the suite pass with a provider name
    # the configuration could never produce.
    name: ClassVar[Provider] = "groq"
    answer: str = '{"reps": 8}'
    usage: tuple[int, int, int] = (11, 7, 0)
    calls: list[ProviderRequest] = field(default_factory=list)

    async def complete(self, request: ProviderRequest) -> ProviderResponse:
        self.calls.append(request)
        return ProviderResponse(
            text=self.answer,
            input_tokens=self.usage[0],
            output_tokens=self.usage[1],
            cached_tokens=self.usage[2],
        )


class Extracted(BaseModel):
    """A schema with no room for extras — spec 7.4 item 5, as a type."""

    model_config = {"extra": "forbid"}

    reps: int


class WithEnum(BaseModel):
    model_config = {"extra": "forbid"}

    effort: Literal["low", "high"]


class Echoing(BaseModel):
    """A schema whose validator writes the rejected value into its message.

    Ordinary Pydantic, and the reason `include_input=False` is not sufficient:
    the text a custom validator raises is quoted verbatim into `msg`.
    """

    model_config = {"extra": "forbid"}

    load_kg: int

    @field_validator("load_kg")
    @classmethod
    def _always_refuses(cls, value: int) -> int:
        raise ValueError(f"segredo do usuario: {value}kg")


class Lax(BaseModel):
    """Pydantic's default — extras are ignored, not refused."""

    reps: int


class LooseChild(BaseModel):
    """Strict outside, permissive inside: the shape `ExtractionResult` has."""

    ok: int


class StrictOuterWithLooseChild(BaseModel):
    model_config = {"extra": "forbid"}

    inner: LooseChild


class StrictInner(BaseModel):
    model_config = {"extra": "forbid"}

    ok: int


class WithDynamicKeys(BaseModel):
    """A mapping field: its *keys* come from the answer, not from the schema."""

    model_config = {"extra": "forbid"}

    tags: dict[str, int]


def gateway(provider: Any, *, models: ModelsConfig | None = None) -> LLMGateway:
    return LLMGateway(models=models or committed_models(), providers={provider.name: provider})


def turn() -> list[BaseMessage]:
    return [SystemMessage(content="regras"), HumanMessage(content="supino 80kg 8 reps")]


# --------------------------------------------------------------------------- #
# Resolution: the caller names an agent and a role (invariant 4)
# --------------------------------------------------------------------------- #


async def test_ainvoke_resolves_model_by_agent_and_role() -> None:
    """The first test of S03-T06, and the one invariant 4 rests on."""
    models = committed_models()
    fake = FakeProvider()

    await gateway(fake, models=models).ainvoke(
        agent="extraction",
        role=LLMRole.EXTRACTOR,
        tenant_id=7,
        messages=turn(),
        schema=Extracted,
    )

    expected = models.resolve(agent="extraction", role=LLMRole.EXTRACTOR)
    assert expected.primary is not None
    assert fake.calls[0].model == expected.primary.model


async def test_the_role_timeout_reaches_the_provider() -> None:
    """Spec 7.1 responsibility 2. The number is configuration, not a constant."""
    models = committed_models()
    fake = FakeProvider()

    await gateway(fake, models=models).ainvoke(
        agent="extraction",
        role=LLMRole.EXTRACTOR,
        tenant_id=7,
        messages=turn(),
        schema=Extracted,
    )

    assert (
        fake.calls[0].timeout_s
        == models.resolve(agent="extraction", role=LLMRole.EXTRACTOR).timeout_s
    )


@pytest.mark.parametrize("missing", ["agent", "role", "tenant_id", "messages"])
async def test_every_required_argument_is_required(missing: str) -> None:
    """Keyword-only and mandatory: an omission is a TypeError at the call site.

    `agent` and `role` answer different questions (spec 7.1) and neither can
    default — a gateway that guessed the role would resolve a cost class the
    caller never chose, and `agent` is what labels every metric of 20.3.
    """
    arguments: dict[str, Any] = {
        "agent": "extraction",
        "role": LLMRole.EXTRACTOR,
        "tenant_id": 7,
        "messages": turn(),
        "schema": Extracted,
    }
    del arguments[missing]

    with pytest.raises(TypeError, match=missing):
        await gateway(FakeProvider()).ainvoke(**arguments)


async def test_an_agent_override_outranks_the_role() -> None:
    """Spec 7.2.1: role is the default, agent is the exception."""
    base = committed_models()
    models = ModelsConfig.model_validate(
        {
            **base.model_dump(),
            "agents": {
                "extraction": {
                    "role": "EXTRACTOR",
                    "primary": {"model": "a-model-only-this-agent-uses"},
                }
            },
        }
    )
    fake = FakeProvider()

    await gateway(fake, models=models).ainvoke(
        agent="extraction",
        role=LLMRole.EXTRACTOR,
        tenant_id=7,
        messages=turn(),
        schema=Extracted,
    )

    assert fake.calls[0].model == "a-model-only-this-agent-uses"


async def test_a_role_without_a_primary_is_refused_by_name() -> None:
    """The JUDGE has no primary on purpose (spec 7.2), and it runs offline.

    Refusing here names the role; the alternative is `None.model` one layer
    down, which reads as a bug in the gateway rather than a call that should
    never have been made.
    """
    with pytest.raises(RoleHasNoPrimaryError, match="JUDGE"):
        await gateway(FakeProvider()).ainvoke(
            agent="analysis",
            role=LLMRole.JUDGE,
            tenant_id=7,
            messages=turn(),
        )


async def test_an_agent_invoked_under_the_wrong_role_is_refused() -> None:
    """`AGENT_ROLES` says which role each agent invokes; the pair is checked.

    With no `agents:` override — the shipped configuration — `resolve()` has
    nothing to compare against and returns whatever role was asked for. So
    `agent="extraction", role=COACH` would quietly buy the reasoning tier for
    an extraction and label the metric `extraction` while doing it: a call-site
    mistake that shows up as a cost line, not as an error.
    """
    with pytest.raises(ValueError, match="extraction"):
        await gateway(FakeProvider()).ainvoke(
            agent="extraction",
            role=LLMRole.COACH,
            tenant_id=7,
            messages=turn(),
            schema=Extracted,
        )


async def test_an_unregistered_agent_is_refused() -> None:
    """A misspelled agent resolves fine and labels every metric with a name
    nothing else in the system uses (spec 20.3).
    """
    with pytest.raises(ValueError, match="extractoin"):
        await gateway(FakeProvider()).ainvoke(
            agent="extractoin",
            role=LLMRole.EXTRACTOR,
            tenant_id=7,
            messages=turn(),
            schema=Extracted,
        )


async def test_every_registered_agent_may_invoke_its_own_role() -> None:
    """The guard must not reject the sixteen pairs that are correct."""
    fake = FakeProvider()
    invocable = {agent: role for agent, role in AGENT_ROLES.items() if role is not LLMRole.JUDGE}

    for agent, role in invocable.items():
        await gateway(fake).ainvoke(agent=agent, role=role, tenant_id=7, messages=turn())

    assert len(fake.calls) == len(invocable)


async def test_an_unconfigured_provider_is_refused_before_the_call() -> None:
    """A deployment missing the provider the role names fails by name.

    The refusal has to come before the call: the alternative is a `None` where
    an adapter belongs, discovered as an `AttributeError` mid-request.
    """
    fake = FakeProvider()
    # A process that runs some other provider, but not the one EXTRACTOR names.
    without_groq = LLMGateway(models=committed_models(), providers={"anthropic": fake})

    with pytest.raises(LookupError, match="groq"):
        await without_groq.ainvoke(
            agent="extraction",
            role=LLMRole.EXTRACTOR,
            tenant_id=7,
            messages=turn(),
            schema=Extracted,
        )

    assert fake.calls == []


# --------------------------------------------------------------------------- #
# Pydantic is the source of truth (spec 7.4 item 5)
# --------------------------------------------------------------------------- #


async def test_the_answer_is_validated_even_when_the_provider_promises_structure() -> None:
    """An extra field is a failure, not a silently-kept extra.

    The *name* of the extra field is the model's invention, so it does not
    appear in the exception — see
    `test_an_invented_field_name_is_redacted_from_the_location`. What survives
    is the error type, which is what says an undeclared field arrived.
    """
    fake = FakeProvider(answer='{"reps": 8, "invented": "by the model"}')

    with pytest.raises(ValueError, match="extra_forbidden"):
        await gateway(fake).ainvoke(
            agent="extraction",
            role=LLMRole.EXTRACTOR,
            tenant_id=7,
            messages=turn(),
            schema=Extracted,
        )


async def test_an_invalid_enum_value_fails_validation() -> None:
    fake = FakeProvider(answer='{"effort": "catastrophic"}')

    with pytest.raises(ValueError, match="effort"):
        await gateway(fake).ainvoke(
            agent="extraction",
            role=LLMRole.EXTRACTOR,
            tenant_id=7,
            messages=turn(),
            schema=WithEnum,
        )


async def test_a_validated_answer_comes_back_parsed() -> None:
    result = await gateway(FakeProvider()).ainvoke(
        agent="extraction",
        role=LLMRole.EXTRACTOR,
        tenant_id=7,
        messages=turn(),
        schema=Extracted,
    )

    assert isinstance(result, LLMResult)
    assert isinstance(result.parsed, Extracted)
    assert result.parsed.reps == 8


async def test_a_call_without_a_schema_returns_text_and_parses_nothing() -> None:
    """Not every role is structured — `voice` writes prose (spec 9.7)."""
    fake = FakeProvider(answer="Boa série. Anotado.")

    result = await gateway(fake).ainvoke(
        agent="voice", role=LLMRole.VOICE, tenant_id=7, messages=turn()
    )

    assert result.parsed is None
    assert result.text == "Boa série. Anotado."


async def test_answer_that_is_not_json_fails_when_a_schema_was_required() -> None:
    fake = FakeProvider(answer="desculpa, não entendi")

    with pytest.raises(ValueError):
        await gateway(fake).ainvoke(
            agent="extraction",
            role=LLMRole.EXTRACTOR,
            tenant_id=7,
            messages=turn(),
            schema=Extracted,
        )


# --------------------------------------------------------------------------- #
# What the result carries, and what it must never print
# --------------------------------------------------------------------------- #


async def test_an_output_budget_declared_by_a_role_reaches_the_provider() -> None:
    """The adapter can honour `max_tokens`; this proves it can *arrive*.

    `_params_of()` serialises a `ModelSpec`, and `ModelSpec` forbids extras —
    so a field the type does not declare could never travel, whatever the
    adapter did with it. Asserting only at the adapter would have tested a
    parameter no configuration could produce.
    """
    base = committed_models()
    models = ModelsConfig.model_validate(
        {
            **base.model_dump(),
            "agents": {
                "analysis": {"role": "ANALYST", "primary": {"max_tokens": 32_000}},
            },
        }
    )
    fake = FakeProvider()

    await gateway(fake, models=models).ainvoke(
        agent="analysis", role=LLMRole.ANALYST, tenant_id=7, messages=turn()
    )

    assert fake.calls[0].params["max_tokens"] == 32_000


async def test_a_validation_failure_never_quotes_the_rejected_value() -> None:
    """`include_input=False` is not enough: a validator's own message can embed
    the value it rejected.

    `Value error, <whatever the validator wrote>` is model output derived from
    the user's prompt, and it would reach a traceback here and OTel once S03-T07
    records exceptions. Only the stable error `type` and the field location
    survive — both are structural, neither is content.
    """
    fake = FakeProvider(answer='{"load_kg": 80}')

    with pytest.raises(ValueError) as raised:
        await gateway(fake).ainvoke(
            agent="extraction",
            role=LLMRole.EXTRACTOR,
            tenant_id=7,
            messages=turn(),
            schema=Echoing,
        )

    assert "80" not in str(raised.value)
    assert "segredo" not in str(raised.value)
    # The diagnosis still names where and what kind.
    assert "load_kg" in str(raised.value)
    assert "value_error" in str(raised.value)


async def test_a_dynamic_key_in_a_location_is_redacted() -> None:
    """`loc` is not always schema-defined: a mapping key comes from the answer.

    Pydantic puts the offending key straight into `loc`, so a model that used
    the user's own words as a key would push them into the exception — and
    into OTel once S03-T07 records failures. Only names the schema declares,
    and structural indices, survive.
    """
    secret = "supino reto com 80kg"
    fake = FakeProvider(answer=f'{{"tags": {{"{secret}": "nao numero"}}}}')

    with pytest.raises(ValueError) as raised:
        await gateway(fake).ainvoke(
            agent="extraction",
            role=LLMRole.EXTRACTOR,
            tenant_id=7,
            messages=turn(),
            schema=WithDynamicKeys,
        )

    assert secret not in str(raised.value)
    # The declared field still names itself, or the message says nothing.
    assert "tags" in str(raised.value)


async def test_an_invented_field_name_is_redacted_from_the_location() -> None:
    """The name of an extra field is the model's invention, not the schema's."""
    fake = FakeProvider(answer='{"reps": 8, "supino reto com 80kg": 1}')

    with pytest.raises(ValueError) as raised:
        await gateway(fake).ainvoke(
            agent="extraction",
            role=LLMRole.EXTRACTOR,
            tenant_id=7,
            messages=turn(),
            schema=Extracted,
        )

    assert "supino" not in str(raised.value)
    assert "extra_forbidden" in str(raised.value)


async def test_a_nested_schema_that_permits_extras_is_refused() -> None:
    """`extra="forbid"` on the outer model says nothing about the inner one.

    `ExtractionResult.sets: list[ExtractedSet]` (spec 9.4) has exactly this
    shape, so a check that stopped at the top level would guarantee nothing
    for the field that actually carries the extraction.
    """
    with pytest.raises(ValueError, match="LooseChild"):
        await gateway(FakeProvider()).ainvoke(
            agent="extraction",
            role=LLMRole.EXTRACTOR,
            tenant_id=7,
            messages=turn(),
            schema=StrictOuterWithLooseChild,
        )


async def test_a_lax_schema_is_refused_before_the_provider_is_called() -> None:
    """A programming error must not cost a paid call whose answer is discarded."""
    fake = FakeProvider()

    with pytest.raises(ValueError):
        await gateway(fake).ainvoke(
            agent="extraction",
            role=LLMRole.EXTRACTOR,
            tenant_id=7,
            messages=turn(),
            schema=Lax,
        )

    assert fake.calls == []


async def test_a_schema_that_permits_extra_fields_is_refused() -> None:
    """The acceptance criterion is the gateway's, not the schema author's.

    Pydantic's default is `extra="ignore"`: a lax schema drops an invented
    field silently, and the sprint's "an extra field fails" would hold only
    because every schema so far happened to declare `forbid`. Checking the
    schema instead of the answer moves the failure to the call site, and
    catches nested models too — a hand-rolled key check at this boundary would
    only ever see the top level.
    """
    with pytest.raises(ValueError, match="extra"):
        await gateway(FakeProvider()).ainvoke(
            agent="extraction",
            role=LLMRole.EXTRACTOR,
            tenant_id=7,
            messages=turn(),
            schema=Lax,
        )


async def test_the_result_carries_what_the_ledger_will_need() -> None:
    """S03-T07 writes `usage_ledger` from this (spec 5.2); T06 supplies it."""
    models = committed_models()
    fake = FakeProvider(usage=(31, 12, 4))

    result = await gateway(fake, models=models).ainvoke(
        agent="extraction",
        role=LLMRole.EXTRACTOR,
        tenant_id=7,
        messages=turn(),
        schema=Extracted,
    )

    resolved = models.resolve(agent="extraction", role=LLMRole.EXTRACTOR)
    assert resolved.primary is not None
    assert result.agent == "extraction"
    assert result.role is LLMRole.EXTRACTOR
    assert result.provider == resolved.primary.provider
    assert result.model == resolved.primary.model
    assert (result.usage.input_tokens, result.usage.output_tokens) == (31, 12)
    assert result.usage.cached_tokens == 4


async def test_no_repr_of_a_result_or_a_request_carries_user_content() -> None:
    """Invariant 10 and spec 20.6: the prompt and the answer are not metadata.

    A traceback prints reprs, and a traceback is the least deliberate place
    content ever reaches. Langfuse gets the text on purpose (spec 20.1); no
    accidental channel gets it at all.
    """
    fake = FakeProvider(answer='{"reps": 8}')

    result = await gateway(fake).ainvoke(
        agent="extraction",
        role=LLMRole.EXTRACTOR,
        tenant_id=7,
        messages=turn(),
        schema=Extracted,
    )

    assert "supino 80kg 8 reps" not in repr(fake.calls[0])
    assert "reps" not in repr(result)
