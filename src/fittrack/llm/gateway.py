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
from fittrack.llm.roles import LLMRole
from fittrack.llm.types import LLMResult, TokenUsage

__all__ = ["LLMGateway", "RoleHasNoPrimaryError", "SchemaViolationError"]


class RoleHasNoPrimaryError(LookupError):
    """A role with no primary model was invoked through the gateway.

    Only the JUDGE is in that state, and deliberately: a judge running on the
    model that produced the answer is not a judge (spec 7.2). It runs offline
    from `evals/`, never through this path — so naming the role here is more
    useful than an `AttributeError` on `None.model` one frame down.
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
            raise RoleHasNoPrimaryError(
                f"role {role.value} has no primary model and is not invocable "
                "through the gateway (spec 7.2)"
            )
        return resolved.primary


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


def _validated(text: str, schema: type[BaseModel] | None) -> BaseModel | None:
    """Pydantic has the last word, whatever the provider promised (7.4 item 5)."""
    if schema is None:
        return None
    try:
        return schema.model_validate_json(text)
    except ValidationError as error:
        # The errors carry field names and the offending values, and the
        # offending value is model output derived from the user's text. The
        # field locations are the diagnosis; the inputs are not ours to print.
        locations = "; ".join(
            ".".join(str(part) for part in detail["loc"]) + f": {detail['msg']}"
            for detail in error.errors(include_url=False, include_input=False)
        )
        raise SchemaViolationError(
            f"answer does not satisfy {schema.__name__}: {locations}"
        ) from None
