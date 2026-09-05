"""LLM layer (spec section 7).

`LLMGateway` is the single entry point for every invocation, and it lives in
`fittrack.llm.gateway` — imported from there, not from this package.

That is a layering constraint, not a style choice. `fittrack.config` types
`models.yaml` and imports `LLMRole` and `AGENT_ROLES` from `roles` below, while
the gateway reads a `ModelsConfig` from `config` above. Re-exporting the
gateway here would put `config` on both sides of the same import and break the
cycle open on the first `import fittrack.config` — which is how this file
learned the rule.

So the package root carries only what sits *below* `config`: the roles.
"""

from fittrack.llm.roles import AGENT_ROLES, KNOWN_AGENTS, LLMRole

__all__ = ["AGENT_ROLES", "KNOWN_AGENTS", "LLMRole"]
