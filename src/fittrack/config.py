"""Typed loading of the versioned YAML configuration.

Three files, three shapes, one rule: **refuse rather than guess.** These files
are the only place model names live (CLAUDE.md, invariant 4), so a typo in one
of them is a production incident and not a configuration wart. Every loader
fails at boot, names the file, and says what was wrong.

Secrets are not here. They come from the environment through `settings.py`;
keeping the two apart is what lets these files be versioned and reloadable
(spec 7.2) while credentials never are.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, Any, Literal, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from fittrack.llm.roles import AGENT_ROLES, KNOWN_AGENTS, LLMRole

Provider = Literal["groq", "anthropic", "openai", "xai"]
ReasoningEffort = Literal["low", "medium", "high"]
PlanTier = Literal["free", "pro", "trial"]


class ConfigError(ValueError):
    """A configuration file does not satisfy its contract."""


class _StrictLoader(yaml.SafeLoader):
    """A safe loader that refuses a duplicate mapping key.

    `yaml.safe_load` keeps the last occurrence and says nothing, so a bad merge
    leaving two `COACH:` blocks would boot with one of them silently discarded —
    and the discarded one is as likely to be the intended one.
    """


def _no_duplicate_keys(
    loader: _StrictLoader, node: yaml.MappingNode, deep: bool = False
) -> dict[Any, Any]:
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise yaml.constructor.ConstructorError(
                None, None, f"duplicate key {key!r}", key_node.start_mark
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_StrictLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _no_duplicate_keys,
)


def _read(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as error:
        raise ConfigError(f"{path.name}: cannot be read ({error.strerror})") from error
    try:
        parsed = yaml.load(raw, Loader=_StrictLoader)
    except yaml.YAMLError as error:
        raise ConfigError(f"{path.name}: {error}") from error
    if not isinstance(parsed, dict):
        raise ConfigError(f"{path.name}: top level must be a mapping")
    return parsed


# --------------------------------------------------------------------------- #
# models.yaml — spec 7.2 and 7.2.1
# --------------------------------------------------------------------------- #


class ModelSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    provider: Provider
    # Non-empty: no provider can route a request to a blank identifier, and the
    # failure would land on the first call rather than at boot.
    model: Annotated[str, Field(min_length=1, pattern=r"\S")]
    reasoning_effort: ReasoningEffort | None = None
    effort: ReasoningEffort | None = None
    temperature: Annotated[float, Field(ge=0.0, le=2.0)] | None = None


class RoleConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    # The JUDGE has no primary on purpose: a judge running on the model that
    # produced the answer is not a judge (spec 7.2).
    primary: ModelSpec | None = None
    fallback: ModelSpec
    # Spec 7.3: the gateway default. 120s for the reasoning tier, which the
    # committed configuration declares explicitly.
    timeout_s: Annotated[int, Field(gt=0)] = 45


class AgentOverride(BaseModel):
    """A partial override of a role, for one agent (spec 7.2.1).

    Only what differs is declared; the rest comes from the role. An override
    that restated the whole role would stop tracking changes to it, which is
    exactly the bug the inheritance exists to avoid.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    # Mandatory: without it there is nobody to inherit from, and the gateway
    # should fail at boot rather than guess.
    role: LLMRole
    primary: dict[str, Any] = Field(default_factory=dict)
    fallback: dict[str, Any] = Field(default_factory=dict)
    timeout_s: Annotated[int, Field(gt=0)] | None = None


class SttConfig(BaseModel):
    """Speech to text (spec 11.1), which is not one of the roles of 7.2.

    It lives in `models.yaml` for one reason: a model identifier must not
    appear in Python (CLAUDE.md, invariant 4). Everything that makes a role a
    role is absent here — no system prompt, no reasoning tier, no
    cross-provider fallback — so giving it an `LLMRole` would put a value in
    the tiering table that the gateway could not resolve and the golden set
    could not evaluate. ADR-0007 records the choice.

    The three numbers are the rules of the table in 11.3, and they are here so
    that changing one is a configuration change rather than a deploy.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    provider: Provider
    model: Annotated[str, Field(min_length=1, pattern=r"\S")]
    language: Annotated[str, Field(min_length=2)] = "pt"
    # A `Literal` rather than a string: `no_speech_prob` and the segments the
    # inaudibility rule of 11.3 reads arrive in no other response format, so a
    # deployment that asked for `json` would silently lose the rule.
    response_format: Literal["verbose_json"] = "verbose_json"
    # The vocabulary of 11.2, by name. The file itself is pt-BR content and
    # lives beside the other prompts (AD-27).
    prompt_file: Annotated[str, Field(min_length=1)] = "stt_vocabulary.md"
    timeout_s: Annotated[int, Field(gt=0)] = 120
    max_audio_seconds: Annotated[int, Field(gt=0)] = 300
    no_speech_threshold: Annotated[float, Field(ge=0.0, le=1.0)] = 0.6
    retry_retention_hours: Annotated[int, Field(gt=0)] = 6


class ModelsConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    roles: dict[LLMRole, RoleConfig]
    agents: dict[str, AgentOverride] = Field(default_factory=dict)
    # Optional in the type, required in the committed file. A partial
    # `models.yaml` — the shape every loader test writes — must still fail on
    # the thing it is testing rather than on a section it does not mention;
    # `require_stt` is what turns absence into an error at the point of use.
    stt: SttConfig | None = None

    @model_validator(mode="after")
    def _every_role_is_present(self) -> Self:
        missing = sorted(role.value for role in LLMRole if role not in self.roles)
        if missing:
            raise ValueError(f"no configuration for role(s): {', '.join(missing)}")
        return self

    @model_validator(mode="after")
    def _only_the_judge_may_lack_a_primary(self) -> Self:
        for role, config in self.roles.items():
            if config.primary is None and role is not LLMRole.JUDGE:
                raise ValueError(f"role {role.value} has no primary")
        if self.roles[LLMRole.JUDGE].primary is not None:
            raise ValueError(
                "JUDGE must have no primary: a judge sharing a model with the "
                "answer under evaluation is not a judge (spec 7.2)"
            )
        return self

    @model_validator(mode="after")
    def _the_fallback_is_a_different_provider(self) -> Self:
        for role, config in self.roles.items():
            _require_distinct_providers(f"role {role.value}", config)
        return self

    @model_validator(mode="after")
    def _overrides_merge_cleanly(self) -> Self:
        """Resolve every override once, at load time.

        `primary` and `fallback` are partial patches, so they cannot be typed as
        `ModelSpec` directly. Left unchecked, `temperature: 99` or a misspelled
        provider would only surface on the first request routed to that agent —
        which is the boot-time failure this file exists to provide, deferred to
        production.
        """
        for name in self.agents:
            # Against the *registered* role, not the declared one: resolving with
            # the override's own role compares it to itself, so an agent
            # configured under the wrong role passes boot and fails on its first
            # call — which is the failure this check exists to move earlier.
            registered = AGENT_ROLES.get(name)
            if registered is None:
                continue  # unknown name; the next validator reports it by name
            try:
                self.resolve(agent=name, role=registered)
            except (ConfigError, ValueError) as error:
                raise ValueError(f"override for agent {name!r} is invalid: {error}") from error
        return self

    @model_validator(mode="after")
    def _overrides_target_registered_agents(self) -> Self:
        unknown = sorted(set(self.agents) - KNOWN_AGENTS)
        if unknown:
            raise ValueError(
                f"override(s) for unregistered agent(s): {', '.join(unknown)}. "
                "A renamed agent leaves dead, silent configuration behind."
            )
        return self

    def require_stt(self) -> SttConfig:
        """The transcription configuration, or a refusal naming the section.

        Voice is not optional in phase 1.0 (spec 24), so a deployment whose
        `models.yaml` has no `stt:` block cannot transcribe anything. Failing
        here names the file; the alternative is a `None` reaching the service
        and an `AttributeError` on the first voice message.
        """
        if self.stt is None:
            raise ConfigError("models.yaml: no stt section (spec 11.1)")
        return self.stt

    def resolve(self, *, agent: str | None, role: LLMRole) -> RoleConfig:
        """Resolution order: `agents.<name>` merged over the role, then the role.

        The role the YAML declares must match the one the caller asks for, so
        the configuration and the code cannot disagree about an agent's cost
        class (spec 7.2.1).
        """
        base = self.roles[role]
        override = self.agents.get(agent) if agent else None
        if override is None:
            return base
        if override.role is not role:
            raise ConfigError(
                f"agent {agent!r} is configured under role {override.role.value} "
                f"but was invoked as {role.value}"
            )
        resolved = RoleConfig(
            primary=_merge(base.primary, override.primary),
            fallback=_merge(base.fallback, override.fallback) or base.fallback,
            timeout_s=override.timeout_s or base.timeout_s,
        )
        # The failover rule has to hold after the merge too: an override that
        # points the fallback at the primary's provider passes the role-level
        # check and still leaves the retry sharing the outage.
        _require_distinct_providers(f"agent {agent}", resolved)
        return resolved


def _require_distinct_providers(what: str, config: RoleConfig) -> None:
    """Spec 7.3: the fallback exists to survive a provider outage.

    Pointing both at one provider leaves the failover path unable to recover
    from the failure it was built for — the retry shares the outage, the rate
    limit and the quota.
    """
    if config.primary is not None and config.primary.provider == config.fallback.provider:
        raise ConfigError(
            f"{what} has the same provider on primary and fallback; "
            "the fallback could not survive a provider outage"
        )


def _merge(base: ModelSpec | None, patch: dict[str, Any]) -> ModelSpec | None:
    if base is None:
        return None
    if not patch:
        return base
    return ModelSpec.model_validate({**base.model_dump(exclude_none=True), **patch})


def load_models(path: Path) -> ModelsConfig:
    try:
        return ModelsConfig.model_validate(_read(path))
    except ConfigError:
        raise
    except ValueError as error:
        raise ConfigError(f"{path.name}: {error}") from error


# --------------------------------------------------------------------------- #
# quota.yaml — spec 19.3
# --------------------------------------------------------------------------- #


class PlanQuota(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    # `allow_inf_nan=False` is the point: YAML accepts `.inf`, and `gt=0` accepts
    # it happily — which would pass boot validation while disabling the very
    # cost ceiling this file exists to impose (spec 19.3).
    llm_usd_month: Annotated[float, Field(gt=0, allow_inf_nan=False)]
    analysis_calls_month: Annotated[int, Field(gt=0)]


class QuotaConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    plans: dict[PlanTier, PlanQuota]

    @model_validator(mode="after")
    def _every_plan_has_a_ceiling(self) -> Self:
        missing = sorted({"free", "pro", "trial"} - set(self.plans))
        if missing:
            raise ValueError(f"no quota for plan(s): {', '.join(missing)}")
        return self


class QuotaFile(BaseModel):
    """The file's own shape.

    Validated before the payload is reshaped, so `extra="forbid"` can still see
    a misspelled sibling section — which would otherwise sit there inert while
    the real ceiling stayed at its old value.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    quota: dict[PlanTier, PlanQuota]


def load_quota(path: Path) -> QuotaConfig:
    try:
        parsed = QuotaFile.model_validate(_read(path))
        return QuotaConfig.model_validate({"plans": parsed.quota})
    except ConfigError:
        raise
    except ValueError as error:
        raise ConfigError(f"{path.name}: {error}") from error


# --------------------------------------------------------------------------- #
# rag.yaml — spec 15.3
# --------------------------------------------------------------------------- #


class EmbeddingsConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    provider: Provider
    # Non-empty, for the same reason as a role's model: no provider can route to
    # a blank identifier, and the failure would land on the first embedding.
    model: Annotated[str, Field(min_length=1, pattern=r"\S")]
    dimensions: Annotated[int, Field(gt=0)]


class HnswConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    m: Annotated[int, Field(gt=0)] = 16
    ef_construct: Annotated[int, Field(gt=0)] = 128


class QdrantConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    distance: Literal["Cosine", "Dot", "Euclid"]
    hnsw: HnswConfig = Field(default_factory=HnswConfig)
    quantization: str | None = None


class RetrievalConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    top_k: Annotated[int, Field(gt=0)]
    score_threshold: Annotated[float, Field(ge=0.0, le=1.0)]
    rerank: bool = False


class RagConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    embeddings: EmbeddingsConfig
    qdrant: QdrantConfig
    retrieval: RetrievalConfig


def load_rag(path: Path) -> RagConfig:
    try:
        return RagConfig.model_validate(_read(path))
    except ConfigError:
        raise
    except ValueError as error:
        raise ConfigError(f"{path.name}: {error}") from error


# --------------------------------------------------------------------------- #
# The bundle
# --------------------------------------------------------------------------- #


class Config(BaseModel):
    """Everything the versioned configuration directory holds."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    models: ModelsConfig
    quota: QuotaConfig
    rag: RagConfig


def load_config(directory: Path) -> Config:
    """Load and cross-validate the three files of `FITTRACK_CONFIG_DIR`.

    Loaded separately from secrets, and deliberately so: these files are
    committed and re-read without a redeploy (spec 7.2), which a credential
    could never be.
    """
    return Config(
        models=load_models(directory / "models.yaml"),
        quota=load_quota(directory / "quota.yaml"),
        rag=load_rag(directory / "rag.yaml"),
    )


def configured_providers(config: Config) -> set[str]:
    """Every provider the versioned configuration actually references.

    Resolved overrides included: one that switches an agent to another provider
    needs that provider's credential, and reporting the deployment as fully
    credentialed would defer the failure to the first request routed there.
    """
    providers: set[str] = {config.rag.embeddings.provider}
    if config.models.stt is not None:
        # Audio leaves the infrastructure through this provider (spec 11.3),
        # and a deployment without its credential fails on the first voice
        # message rather than at boot.
        providers.add(config.models.stt.provider)
    roles = list(config.models.roles.values())
    roles += [
        config.models.resolve(agent=name, role=override.role)
        for name, override in config.models.agents.items()
    ]
    for role in roles:
        for spec in (role.primary, role.fallback):
            if spec is not None:
                providers.add(spec.provider)
    return providers


def missing_provider_credentials(
    config: Config, credentials: Mapping[str, str | None]
) -> list[str]:
    """Providers the configuration names but for which no credential is set.

    A deployment in that state validates cleanly and then fails on its first
    LLM or embedding call. This is the check; the component that makes those
    calls is the one that raises on it, so the failure names what was about to
    be used rather than something nothing touches yet.
    """
    return sorted(
        provider for provider in configured_providers(config) if not credentials.get(provider)
    )
