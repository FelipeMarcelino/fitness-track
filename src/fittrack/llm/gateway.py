"""The single entry point for every LLM invocation (spec 7.1).

`ainvoke` takes an **agent** and a **role** and nothing that names a model.
That is invariant 4 expressed as a signature: the identifier comes from
`config/models.yaml` through `ModelsConfig.resolve()`, so a caller that wanted
a particular model would have nowhere to put the request. `role` answers "what
class of model does this need"; `agent` answers "who is calling", and it is
mandatory because every metric of 20.3 is labelled by it and 7.2.1's override
is keyed on it.

`tenant_id` is an argument for the same family of reasons as invariant 3: it is
supplied by the code that already knows it, and no model output can influence
it.

**What this gateway does not do yet.** Spec 7.1 lists six responsibilities;
S03-T06 implements resolution (1), the timeout (2) and structured-output
normalisation (4). Retry and cross-provider fallback (3), the `usage_ledger`
write and tracing (5) and the quota check (6) arrive with S03-T07, which wraps
this call path rather than replacing it — `LLMResult` already carries the
tokens that the ledger row needs. `tools` and `trace_ctx` from the 7.1
signature land with the tasks that first have a caller for them: no phase-1.0
role uses tools (7.4 item 3), and tracing has no consumer until T07.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from langchain_core.messages import BaseMessage
from pydantic import BaseModel, ValidationError

from fittrack.config import ModelsConfig, ModelSpec
from fittrack.llm.providers.base import LLMProvider, ProviderRequest
from fittrack.llm.roles import AGENT_ROLES, LLMRole
from fittrack.llm.types import LLMResult, TokenUsage

__all__ = [
    "LLMGateway",
    "RoleHasNoPrimaryError",
    "SchemaViolationError",
    "UnknownAgentError",
]


class RoleHasNoPrimaryError(LookupError):
    """A role with no primary model was invoked through the gateway.

    Only the JUDGE is in that state, and deliberately: a judge running on the
    model that produced the answer is not a judge (spec 7.2). It runs offline
    from `evals/`, never through this path — so naming the role here is more
    useful than an `AttributeError` on `None.model` one frame down.
    """


class UnknownAgentError(ValueError):
    """The `(agent, role)` pair is not one `AGENT_ROLES` registers.

    A `ValueError` rather than a `LookupError`: this is a bad argument at the
    call site, not a missing entry the caller could not have known about.
    """


class SchemaViolationError(ValueError):
    """The answer did not satisfy the schema the caller required.

    Raised for a malformed body and for a well-formed one carrying a field the
    schema does not declare, because 7.4 item 5 makes no distinction: the
    Pydantic validation is the source of truth, not the provider's promise of
    structured output.
    """


class LLMGateway:
    """Resolves `(agent, role)`, calls one provider, validates the answer."""

    def __init__(
        self,
        *,
        models: ModelsConfig,
        providers: Mapping[str, LLMProvider],
    ) -> None:
        self._models = models
        self._providers = dict(providers)

    async def ainvoke(
        self,
        *,
        agent: str,
        role: LLMRole,
        tenant_id: int,
        messages: Sequence[BaseMessage],
        schema: type[BaseModel] | None = None,
    ) -> LLMResult:
        """One invocation, resolved by role and validated by Pydantic.

        `tenant_id` is required and unused here on purpose: it is what the
        quota check of 7.1 responsibility 6 and the ledger row of 5.2 are keyed
        on, both landing in S03-T07. Making it optional now would mean changing
        every call site later, and the alternative — resolving it from the
        graph state inside the gateway — is what invariant 3 forbids.
        """
        spec = self._primary_for(agent=agent, role=role)
        provider = self._providers.get(spec.provider)
        if provider is None:
            configured = ", ".join(sorted(self._providers)) or "none"
            raise LookupError(
                f"role {role.value} resolves to provider {spec.provider!r}, "
                f"which this process does not run (configured: {configured})"
            )

        request = ProviderRequest(
            model=spec.model,
            messages=tuple(messages),
            params=_params_of(spec),
            schema=schema,
            timeout_s=self._models.resolve(agent=agent, role=role).timeout_s,
        )
        answer = await provider.complete(request)

        return LLMResult(
            text=answer.text,
            parsed=_validated(answer.text, schema),
            provider=spec.provider,
            model=spec.model,
            usage=TokenUsage(
                input_tokens=answer.input_tokens,
                output_tokens=answer.output_tokens,
                cached_tokens=answer.cached_tokens,
            ),
            agent=agent,
            role=role,
        )

    def _primary_for(self, *, agent: str, role: LLMRole) -> ModelSpec:
        resolved = self._models.resolve(agent=agent, role=role)
        if resolved.primary is None:
            # Checked before the pair, and the order matters: "is this role
            # invocable at all" is a property of the role alone, while the
            # check below is about the pairing. The JUDGE reaches this and
            # nothing else can — `ModelsConfig` refuses at boot any other role
            # without a primary.
            raise RoleHasNoPrimaryError(
                f"role {role.value} has no primary model and is not invocable "
                "through the gateway (spec 7.2)"
            )
        _require_registered_pair(agent=agent, role=role)
        return resolved.primary


def _require_registered_pair(*, agent: str, role: LLMRole) -> None:
    """The agent must be registered, and under the role it is invoking.

    `ModelsConfig.resolve()` compares the two only when an `agents:` override
    exists — and the shipped configuration has none (7.2.1 recommends none for
    phase 1.0). So without this, `agent="extraction", role=COACH` resolves
    happily: it buys the reasoning tier for an extraction and labels the cost
    `extraction` while doing it. That is a call-site mistake that surfaces as a
    line on the bill rather than as an error, which is the shape of failure
    `AGENT_ROLES` was built to prevent — its own docstring says so about the
    configuration side of the same mapping.
    """
    registered = AGENT_ROLES.get(agent)
    if registered is None:
        raise UnknownAgentError(
            f"{agent!r} is not a registered agent; every LLM caller is in "
            "AGENT_ROLES (spec 9.2), and the name labels every metric of 20.3"
        )
    if registered is not role:
        raise UnknownAgentError(
            f"agent {agent!r} invokes role {registered.value}, not {role.value} (spec 7.2.1)"
        )


def _params_of(spec: ModelSpec) -> dict[str, object]:
    """The sampling and reasoning knobs the configuration set, and only those.

    Built from the declared fields rather than `model_dump()` so that a `None`
    — the shape of "this role did not set it" — never reaches a provider as an
    explicit null. `provider` and `model` are addressing, not parameters.
    """
    declared = spec.model_dump(exclude_none=True)
    declared.pop("provider", None)
    declared.pop("model", None)
    return declared


def _require_strict(schema: type[BaseModel]) -> None:
    """The schema must refuse undeclared fields, and the gateway checks that.

    Pydantic's default is `extra="ignore"`: a lax schema drops an invented
    field silently, so "an answer with an extra field fails" would be a
    property of whoever wrote the schema rather than of this boundary.

    Checked on the schema rather than enforced on the answer, because the
    alternative — scanning the parsed JSON for undeclared keys — only ever
    sees the top level. `extra="forbid"` is enforced by Pydantic at every
    level of nesting, and a missing declaration is a programming error that
    should surface on the first test that calls the agent, not on the first
    production answer that happens to invent a field.
    """
    if schema.model_config.get("extra") != "forbid":
        raise SchemaViolationError(
            f"{schema.__name__} must declare extra='forbid': the gateway "
            "guarantees that an answer carrying an undeclared field fails "
            "(spec 7.4 item 5), and Pydantic ignores extras by default"
        )


def _validated(text: str, schema: type[BaseModel] | None) -> BaseModel | None:
    """Pydantic has the last word, whatever the provider promised (7.4 item 5)."""
    if schema is None:
        return None
    _require_strict(schema)
    try:
        return schema.model_validate_json(text)
    except ValidationError as error:
        # The location and the error *type*, never the message. `msg` is
        # written by the validator, and a custom one embeds the value it
        # rejected — `Value error, <the model's text>` — which is content
        # derived from the user's prompt. `include_input=False` does not
        # remove it, because it is not the input field: it is the sentence
        # the validator wrote about the input.
        #
        # `type` is a stable machine-readable code (`extra_forbidden`,
        # `int_parsing`, `value_error`) and `loc` is structure. Together they
        # say where and what kind, which is the whole diagnosis worth having
        # here — the answer itself belongs in Langfuse (spec 20.1), on purpose.
        failures = "; ".join(
            ".".join(str(part) for part in detail["loc"]) + f": {detail['type']}"
            for detail in error.errors(include_url=False, include_input=False)
        )
        raise SchemaViolationError(
            f"answer does not satisfy {schema.__name__}: {failures}"
        ) from None
